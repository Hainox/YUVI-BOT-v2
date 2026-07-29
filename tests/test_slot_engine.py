"""Тесты слота "Azumanga" (04.1-02): server-authoritative порт
webapp/slot-data.jsx на Python. Доказывают:

- PAYTABLE == D-06 (пересчитанные числа, RTP ~92.78%), НЕ исходные числа
  черновика webapp/slot-data.jsx (там были другие, "сломанные" по RTP ~39%
  цифры) — источник истины: .planning/phases/04-mini-app-games-mini-app/04-CONTEXT.md
- WEIGHTS == D-05 (веса символов НЕ менялись при пересчёте паутейбла).
- wild (muscle) подставляется вместо любого символа кроме scatter.
- scatter (keffiyeh) платит ТОЛЬКО фриспинами, не по линиям.
- spin_grid возвращает корректную форму сетки 3x5.
- Сэмплированный RTP укладывается в широкий диапазон вокруг 92.78% (включая
  эффект ретриггеров фриспинов во время бонуса, см. ниже).
- Ретриггер фриспинов: >=3 scatter НА бонусном спине добавляет ещё фриспинов
  к остатку (тот же FREESPIN_TABLE), `freespins` в результате — ИТОГО сыграно
  (изначальные + все ретриггеры), с хардкапом FREESPINS_HARD_CAP на суммарную
  выдачу за один вызов `evaluate_grid`, исключающим неограниченный цикл.
- play_slots settles через идемпотентное ядро 04.1-01 (`_settle`), выигрыш
  урезается до остатка банка (D-06).

Все RNG-зависимые тесты форсируют исход через monkeypatch модульного seam
`casino_service._rng` (тот же паттерн, что tests/test_casino_service.py) —
никогда не проверяем реальную случайность напрямую, кроме сэмплированного
RTP-теста, который сознательно использует реальный `slot_engine._rng`-подобный
взвешенный сэмплинг с фиксированным большим N и допуском по диапазону, а не
точным значением.
"""

from __future__ import annotations

import random

import pytest
from sqlalchemy import select

from bot.config import settings
from bot.services import casino_service
from bot.services import economy_service
from bot.services import slot_engine
from bot.data import slot_data
from common.models.casino_game import CasinoGame
from common.models.chat_bank import ChatBank
from common.models.user import User
from common.models.user_balance import UserBalance


async def _ensure_user(session, user_id: int, first_name: str = "Тест") -> None:
    session.add(User(id=user_id, first_name=first_name))
    await session.flush()


async def _fund(session, chat_id: int, user_id: int) -> int:
    return await economy_service.get_balance(session, chat_id, user_id)


async def _get_user_balance(session, chat_id: int, user_id: int) -> int:
    result = await session.execute(
        select(UserBalance.balance).where(
            UserBalance.chat_id == chat_id, UserBalance.user_id == user_id
        )
    )
    return result.scalar_one()


async def _get_bank_balance(session, chat_id: int) -> int:
    result = await session.execute(select(ChatBank.balance).where(ChatBank.chat_id == chat_id))
    return result.scalar_one_or_none() or 0


async def _get_casino_game(session, user_id: int, idem_key: str) -> CasinoGame:
    return (
        await session.execute(
            select(CasinoGame).where(
                CasinoGame.user_id == user_id, CasinoGame.idem_key == idem_key
            )
        )
    ).scalar_one()


class _ForcedGridRng:
    """RNG-стаб, возвращающий заранее заданную последовательность reel-выборов,
    чтобы форсировать конкретную сетку в spin_grid."""

    def __init__(self, choices_sequence):
        self._seq = list(choices_sequence)
        self._i = 0

    def choice(self, seq):
        # Игнорируем seq (реальный пул символов) и отдаём следующий
        # запрограммированный символ по порядку вызовов.
        value = self._seq[self._i % len(self._seq)]
        self._i += 1
        return value

    def randint(self, a: int, b: int) -> int:
        # play_slots также бросает через этот же _rng джекпот-кубик
        # (jackpot_service.contribute_and_maybe_award) — b гарантированно
        # никогда не 1 (slot_jackpot_odds=10000), тесты сеток не должны
        # случайно сорвать джекпот.
        return b


# --- D-06 паутейбл (source-of-truth assertion) -------------------------------


def test_paytable_matches_D06_exactly():
    assert slot_data.PAYTABLE == {
        "muscle": {3: 120, 4: 480, 5: 2400},
        "keffiyeh": {3: 0, 4: 0, 5: 0},
        "gasp": {3: 24, 4: 65, 5: 173},
        "lightning-eyes": {3: 22, 4: 48, 5: 120},
        "dog": {3: 12, 4: 26, 5: 58},
        "osaka-stand": {3: 10, 4: 24, 5: 48},
        "bath-chibi": {3: 7, 4: 19, 5: 36},
        "sakaki": {3: 7, 4: 14, 5: 29},
    }


def test_weights_match_D05():
    assert slot_data.WEIGHTS == {
        "muscle": 2,
        "keffiyeh": 2,
        "gasp": 4,
        "lightning-eyes": 5,
        "dog": 7,
        "osaka-stand": 10,
        "bath-chibi": 9,
        "sakaki": 8,
    }


# --- Wild / Scatter -----------------------------------------------------------


def test_wild_substitutes():
    # Payline 0 (row 1 across all 5 columns): [muscle, muscle, dog, dog, dog]
    grid = [
        ["sakaki", "sakaki", "sakaki", "sakaki", "sakaki"],
        ["muscle", "muscle", "dog", "dog", "dog"],
        ["bath-chibi", "bath-chibi", "bath-chibi", "bath-chibi", "bath-chibi"],
    ]
    result = slot_engine.evaluate_grid(grid, bet_per_line=1)
    dog_wins = [w for w in result.line_wins if w["symbol"] == "dog"]
    assert len(dog_wins) == 1
    win = dog_wins[0]
    assert win["count"] == 5
    assert win["payout"] == slot_data.PAYTABLE["dog"][5] * 1


# --- _line_payout: граница count<3 (None) vs count==3 (платит) --------------


def test_line_payout_two_in_a_row_pays_nothing():
    """Граница `count < 3` -> None. Ровно 2 подряд "dog" от левого края, затем
    разрыв на "gasp" - единственный явный payline-тест (test_wild_substitutes)
    форсирует 5-в-ряд через wild, ни один тест не проверял именно "2 подряд,
    без выплаты"."""
    on_line = ["dog", "dog", "gasp", "dog", "dog"]
    assert slot_engine._line_payout(on_line, bet_per_line=1) is None


def test_line_payout_exactly_three_in_a_row_pays_paytable_three():
    """Граница `count == 3` -> платит PAYTABLE[symbol][3] * bet_per_line, а не
    0 и не ошибочно [4]/[5]. Ровно 3 подряд "dog" от левого края, затем разрыв
    на "gasp" - если `count < 3` случайно станет `count <= 3` (off-by-one),
    этот тест упадёт, хотя test_wild_substitutes (проверяет только count==5)
    и test_sampled_rtp_in_band (широкий допуск RTP) его не заметят."""
    on_line = ["dog", "dog", "dog", "gasp", "gasp"]
    win = slot_engine._line_payout(on_line, bet_per_line=1)
    assert win is not None
    assert win["count"] == 3
    assert win["symbol"] == "dog"
    assert win["payout"] == slot_data.PAYTABLE["dog"][3] * 1


def test_scatter_pays_freespins_not_lines(monkeypatch):
    # >=3 keffiyeh anywhere on the grid -> freespins, 0 line payout for keffiyeh.
    # Forces the auto-played bonus spins (evaluate_grid's module-level _rng,
    # normally secrets.SystemRandom() - see slot_engine.py) to an all-"sakaki"
    # sequence so none of them can themselves roll >=3 scatter and retrigger -
    # this test is only about the INITIAL trigger's freespins count, not
    # retrigger behavior (see test_mid_bonus_retrigger_extends_total below for
    # that). Without this, the real system RNG occasionally rolled a retrigger
    # on a bonus spin, making result.freespins > FREESPIN_TABLE[3] and failing
    # this assertion intermittently (flaky in CI, unrelated to the hardcoded
    # input grid above).
    monkeypatch.setattr(slot_engine, "_rng", _ForcedGridRng(["sakaki"] * 15))

    grid = [
        ["keffiyeh", "sakaki", "keffiyeh", "sakaki", "sakaki"],
        ["sakaki", "sakaki", "sakaki", "sakaki", "keffiyeh"],
        ["sakaki", "sakaki", "sakaki", "sakaki", "sakaki"],
    ]
    result = slot_engine.evaluate_grid(grid, bet_per_line=1)
    keffiyeh_line_wins = [w for w in result.line_wins if w["symbol"] == "keffiyeh"]
    assert keffiyeh_line_wins == []
    assert result.scatter_count == 3
    assert result.freespins == slot_data.FREESPIN_TABLE[3]


def test_scatter_freespin_table_4_and_5():
    assert slot_data.FREESPIN_TABLE == {3: 4, 4: 6, 5: 7}


# --- Bonus retrigger (>=3 scatter on a bonus spin extends the bonus round) ----


def test_mid_bonus_retrigger_extends_total(monkeypatch):
    """Стартовая сдача даёт 3 scatter -> 4 фриспина (D-05). Форсируем, что
    ВТОРОЙ бонусный спин сам содержит 3 scatter -> ретриггер должен добавить
    ещё FREESPIN_TABLE[3]=4 к остатку, и итоговый `freespins` (ИТОГО сыгранных
    бонусных спинов) должен вырасти до 4+4=8, а не остаться на изначальных 4."""
    trigger_grid = [
        ["keffiyeh", "sakaki", "keffiyeh", "sakaki", "sakaki"],
        ["sakaki", "sakaki", "sakaki", "sakaki", "keffiyeh"],
        ["sakaki", "sakaki", "sakaki", "sakaki", "sakaki"],
    ]
    # spin_grid делает 15 rng.choice(...) на сетку (5 столбцов x 3 строки,
    # column-major порядок, см. slot_engine.spin_grid) — раскладываем
    # forced-последовательность по бонусным спинам вручную.
    no_scatter_spin = ["sakaki"] * 15
    retrigger_spin = [
        "keffiyeh", "sakaki", "sakaki",
        "sakaki", "sakaki", "sakaki",
        "keffiyeh", "sakaki", "sakaki",
        "sakaki", "sakaki", "sakaki",
        "keffiyeh", "sakaki", "sakaki",
    ]
    # Бонусный спин #1 — без scatter, #2 — ретриггер (3 scatter), #3..#8 — без
    # scatter (после ретриггера остаток спинов вырастает до 4+4=8 суммарно).
    forced_sequence = no_scatter_spin + retrigger_spin + no_scatter_spin * 6
    monkeypatch.setattr(slot_engine, "_rng", _ForcedGridRng(forced_sequence))

    result = slot_engine.evaluate_grid(trigger_grid, bet_per_line=1)

    assert result.scatter_count == 3
    initial_award = slot_data.FREESPIN_TABLE[3]
    assert result.retrigger_awards == [slot_data.FREESPIN_TABLE[3]]
    assert result.freespins == initial_award + slot_data.FREESPIN_TABLE[3] == 8


def test_bonus_retrigger_hard_cap_stops_unbounded_growth(monkeypatch):
    """Патологический стрик: КАЖДЫЙ бонусный спин (форсированно) содержит
    scatter >= 3 -> без хардкапа remaining бы только рос (net +6 на каждой
    итерации: -1 сыгранный + 7 добавленных) и цикл никогда бы не завершился.
    С хардкапом (`FREESPINS_HARD_CAP=100`) ретриггеры перестают засчитываться,
    как только суммарная выдача уже достигла/превысила бы порог, а уже
    накопленный остаток доигрывается до нуля — цикл гарантированно
    завершается, и итоговое число сыгранных спинов остаётся <= порога."""
    trigger_grid = [
        ["keffiyeh", "sakaki", "keffiyeh", "sakaki", "sakaki"],
        ["sakaki", "sakaki", "sakaki", "sakaki", "keffiyeh"],
        ["sakaki", "sakaki", "sakaki", "sakaki", "sakaki"],
    ]
    # 15 scatter подряд -> _count_scatter=15 (>=5) -> FREESPIN_TABLE[5]=7 на
    # КАЖДОМ бонусном спине. _ForcedGridRng циклит эту 15-символьную
    # последовательность бесконечно (по модулю), так что ЛЮБОЕ число бонусных
    # спинов, сколько бы их ни было сыграно, будет "видеть" >=3 scatter.
    always_scatter_spin = ["keffiyeh"] * 15
    monkeypatch.setattr(slot_engine, "_rng", _ForcedGridRng(always_scatter_spin))

    result = slot_engine.evaluate_grid(trigger_grid, bet_per_line=1)

    assert result.freespins <= slot_engine.FREESPINS_HARD_CAP
    initial_award = slot_data.FREESPIN_TABLE[3]
    assert initial_award + sum(result.retrigger_awards) == result.freespins
    # Каждый ЕДИНСТВЕННЫЙ бонусный спин форсированно ретриггерящий, но
    # засчитанных ретриггеров в разы меньше, чем сыгранных спинов — значит
    # хардкап реально подавил дальнейшие ретриггеры, а не просто "повезло"
    # закончиться естественно.
    assert len(result.retrigger_awards) < result.freespins
    assert result.freespins == 95
    assert result.retrigger_awards == [slot_data.FREESPIN_TABLE[5]] * 13


def test_bonus_retrigger_accepts_exact_hard_cap_equality(monkeypatch):
    """Аудит-регрессия (boundary-condition): оба существующих hard-cap теста
    (выше, и test_freespin_rounds_hard_cap_matches_retrigger_awards ниже)
    форсируют ТОЛЬКО ветку "явный перебор, отклонить" (95+7=102>100 в их
    сценарии) - код-путь `total_awarded + award == FREESPINS_HARD_CAP` (равно
    точно, `<=` должен ПРИНЯТЬ) ни разу не задет. Форсируем, что КАЖДЫЙ
    бонусный спин содержит РОВНО 4 scatter (award=FREESPIN_TABLE[4]=6):
    initial 4 (3-scatter триггер) + 6*16 == 100 == FREESPINS_HARD_CAP ровно
    на 16-м ретриггере (total_awarded ДО него 4+6*15=94, проверка 94+6<=100
    истинна). Если `<=` когда-нибудь заменят на `<` (правдоподобная опечатка),
    16-й ретриггер был бы неправомерно отклонён (freespins=94,
    retrigger_awards - 15 записей вместо 16) - ни один из существующих
    hard-cap тестов (другой числовой сценарий, award=7) этого не заметит."""
    trigger_grid = [
        ["keffiyeh", "sakaki", "keffiyeh", "sakaki", "sakaki"],
        ["sakaki", "sakaki", "sakaki", "sakaki", "keffiyeh"],
        ["sakaki", "sakaki", "sakaki", "sakaki", "sakaki"],
    ]
    # Ровно 4 keffiyeh (scatter) + 11 sakaki на каждом бонусном спине (не 3, не
    # >=5) -> _count_scatter==4 -> award=FREESPIN_TABLE[4]=6 форсированно на
    # КАЖДОМ бонусном спине (_ForcedGridRng циклит эти 15 символов по модулю).
    four_scatter_spin = ["keffiyeh"] * 4 + ["sakaki"] * 11
    monkeypatch.setattr(slot_engine, "_rng", _ForcedGridRng(four_scatter_spin))

    result = slot_engine.evaluate_grid(trigger_grid, bet_per_line=1)

    initial_award = slot_data.FREESPIN_TABLE[3]
    retrigger_award = slot_data.FREESPIN_TABLE[4]
    assert retrigger_award == 6
    assert result.retrigger_awards == [retrigger_award] * 16
    assert initial_award + sum(result.retrigger_awards) == slot_engine.FREESPINS_HARD_CAP == 100
    assert result.freespins == 100


# --- freespin_rounds (запрошено 2026-07-28: полный прокрут каждого бонусного --
# спина в miniapp вместо мгновенного бампа счётчика — фронту нужна сетка/
# выигрыш/скаттер КАЖДОГО реально сыгранного бонусного спина, не только итог) --


def test_freespin_rounds_one_entry_per_played_bonus_spin(monkeypatch):
    """len(freespin_rounds) == freespins (ИТОГО сыграно, включая ретриггер) —
    та же сдача/форсированная последовательность, что test_mid_bonus_
    retrigger_extends_total, но проверяет НОВОЕ поле, а не freespins/
    retrigger_awards."""
    trigger_grid = [
        ["keffiyeh", "sakaki", "keffiyeh", "sakaki", "sakaki"],
        ["sakaki", "sakaki", "sakaki", "sakaki", "keffiyeh"],
        ["sakaki", "sakaki", "sakaki", "sakaki", "sakaki"],
    ]
    no_scatter_spin = ["sakaki"] * 15
    retrigger_spin = [
        "keffiyeh", "sakaki", "sakaki",
        "sakaki", "sakaki", "sakaki",
        "keffiyeh", "sakaki", "sakaki",
        "sakaki", "sakaki", "sakaki",
        "keffiyeh", "sakaki", "sakaki",
    ]
    forced_sequence = no_scatter_spin + retrigger_spin + no_scatter_spin * 6
    monkeypatch.setattr(slot_engine, "_rng", _ForcedGridRng(forced_sequence))

    result = slot_engine.evaluate_grid(trigger_grid, bet_per_line=1)

    assert len(result.freespin_rounds) == result.freespins == 8
    for round_ in result.freespin_rounds:
        assert set(round_) == {"grid", "wins", "payout", "scatter_count", "retrigger_award"}
        assert len(round_["grid"]) == 3
        assert all(len(row) == 5 for row in round_["grid"])

    # Только ВТОРОЙ сыгранный бонусный спин (индекс 1) реально ретриггерил —
    # тот же порядок, что forced_sequence: no_scatter_spin -> retrigger_spin -> ...
    retrigger_flags = [r["retrigger_award"] > 0 for r in result.freespin_rounds]
    assert retrigger_flags == [False, True, False, False, False, False, False, False]
    assert result.freespin_rounds[1]["retrigger_award"] == slot_data.FREESPIN_TABLE[3]
    assert result.freespin_rounds[1]["scatter_count"] == 3


def test_freespin_rounds_payouts_sum_into_total_payout():
    """Сумма payout всех freespin_rounds + линейный выигрыш стартовой сетки ==
    result.total_payout — freespin_rounds не теряет и не задваивает деньги
    относительно уже проверенного total_payout (никакой НОВОЙ денежной логики
    это поле не вводит, только раскрывает уже посчитанное по раундам)."""
    rng = random.Random(777)
    grid = slot_engine.spin_grid(rng)
    result = slot_engine.evaluate_grid(grid, bet_per_line=5)

    _, line_total = slot_engine._sum_line_wins(result.grid, bet_per_line=5)
    rounds_total = sum(r["payout"] for r in result.freespin_rounds)
    assert line_total + rounds_total == result.total_payout


def test_freespin_rounds_empty_without_scatter():
    """Без >=3 scatter на стартовой сетке — freespins=0 и freespin_rounds
    пуст (не None, не список с "пустыми" заглушками)."""
    grid = [
        ["sakaki", "sakaki", "sakaki", "sakaki", "sakaki"],
        ["muscle", "muscle", "dog", "dog", "dog"],
        ["bath-chibi", "bath-chibi", "bath-chibi", "bath-chibi", "bath-chibi"],
    ]
    result = slot_engine.evaluate_grid(grid, bet_per_line=1)
    assert result.freespins == 0
    assert result.freespin_rounds == []


def test_freespin_rounds_hard_cap_matches_retrigger_awards(monkeypatch):
    """Тот же патологический стрик, что test_bonus_retrigger_hard_cap_stops_
    unbounded_growth — freespin_rounds должен согласовываться с уже
    проверенным result.retrigger_awards: ровно len(retrigger_awards) записей
    с retrigger_award > 0, суммарно равным sum(retrigger_awards) (хардкап
    подавляет ИЗБЫТОЧНЫЕ ретриггеры одинаково что в агрегате, что по раундам)."""
    trigger_grid = [
        ["keffiyeh", "sakaki", "keffiyeh", "sakaki", "sakaki"],
        ["sakaki", "sakaki", "sakaki", "sakaki", "keffiyeh"],
        ["sakaki", "sakaki", "sakaki", "sakaki", "sakaki"],
    ]
    always_scatter_spin = ["keffiyeh"] * 15
    monkeypatch.setattr(slot_engine, "_rng", _ForcedGridRng(always_scatter_spin))

    result = slot_engine.evaluate_grid(trigger_grid, bet_per_line=1)

    assert len(result.freespin_rounds) == result.freespins == 95
    nonzero = [r["retrigger_award"] for r in result.freespin_rounds if r["retrigger_award"] > 0]
    assert len(nonzero) == len(result.retrigger_awards) == 13
    assert sum(nonzero) == sum(result.retrigger_awards)


# --- Grid shape ---------------------------------------------------------------


def test_grid_shape():
    rng = random.Random(42)
    grid = slot_engine.spin_grid(rng)
    assert len(grid) == 3
    for row in grid:
        assert len(row) == 5
        for symbol in row:
            assert symbol in slot_data.SYMBOLS


# --- Sampled RTP ---------------------------------------------------------------


def test_sampled_rtp_in_band():
    rng = random.Random(12345)
    n_spins = 200_000
    bet_per_line = 1
    total_bet = 0
    total_payout = 0
    for _ in range(n_spins):
        grid = slot_engine.spin_grid(rng)
        result = slot_engine.evaluate_grid(grid, bet_per_line=bet_per_line)
        total_bet += bet_per_line * slot_engine.TOTAL_LINES
        total_payout += result.total_payout

    rtp = total_payout / total_bet
    assert 0.88 <= rtp <= 0.97, f"RTP {rtp} out of expected band"


# --- play_slots settle wiring ---------------------------------------------------


@pytest.mark.asyncio
async def test_play_slots_settles_and_is_idempotent(session, monkeypatch):
    chat_id = -100900101
    user_id = 900101
    await _ensure_user(session, user_id)
    balance_before = await _fund(session, chat_id, user_id)
    await economy_service.credit_bank(
        session, chat_id, 1_000_000, kind="test_seed", ref_id="test_slot_seed_bank"
    )
    await session.commit()

    # Форсируем сетку без выигрышных линий и без скаттера (payout=0), проще
    # доказать: одно движение денег, одна строка CasinoGame(game="slots").
    # play_slots передаёт в slot_engine.spin_grid ИМЕННО casino_service._rng
    # (общий seam settle-ядра 04.1-01), а не отдельный rng slot_engine.
    forced_symbols = ["sakaki", "bath-chibi", "osaka-stand", "dog", "gasp"] * 3
    monkeypatch.setattr(casino_service, "_rng", _ForcedGridRng(forced_symbols))

    idem_key = "test_slot_idempotent"
    bet_total = 10 * slot_engine.TOTAL_LINES

    first = await casino_service.play_slots(session, chat_id, user_id, bet_total, idem_key)
    balance_after_first = await _get_user_balance(session, chat_id, user_id)
    # Новый спин -> jackpot_service отработал (пул реально пополнился).
    assert first["jackpot"] == {
        "won": False,
        "pool": settings.slot_jackpot_seed + int(bet_total * settings.slot_jackpot_skim_pct),
    }

    game = await _get_casino_game(session, user_id, idem_key)
    assert game.game == "slots"
    assert "grid" in game.outcome
    assert "wins" in game.outcome

    second = await casino_service.play_slots(session, chat_id, user_id, bet_total, idem_key)
    assert second["payout"] == first["payout"]
    assert second["outcome"] == first["outcome"]
    assert await _get_user_balance(session, chat_id, user_id) == balance_after_first
    # Replay того же idem_key -> джекпот НЕ трогается второй раз (не растёт,
    # кубик не бросается повторно).
    assert second["jackpot"] is None

    games = (
        await session.execute(
            select(CasinoGame).where(
                CasinoGame.user_id == user_id, CasinoGame.idem_key == idem_key
            )
        )
    ).scalars().all()
    assert len(games) == 1


@pytest.mark.asyncio
async def test_play_slots_payout_capped_to_bank(session, monkeypatch):
    chat_id = -100900102
    user_id = 900102
    await _ensure_user(session, user_id)
    await _fund(session, chat_id, user_id)

    small_bank = 5
    await economy_service.credit_bank(
        session, chat_id, small_bank, kind="test_seed", ref_id="test_slot_bank_cap_seed"
    )
    await session.commit()

    # Форсируем 5-в-ряд muscle (wild) на каждой из 10 линий -> максимальный
    # возможный выигрыш (jackpot), заведомо превышающий крошечный банк.
    forced_symbols = ["muscle"] * 15
    monkeypatch.setattr(casino_service, "_rng", _ForcedGridRng(forced_symbols))

    bet_total = 10 * slot_engine.TOTAL_LINES
    result = await casino_service.play_slots(
        session, chat_id, user_id, bet_total, "test_slot_bank_cap"
    )

    bank_after = await _get_bank_balance(session, chat_id)
    assert bank_after >= 0
    # Ставка ушла в банк (bank += bet_total) до выплаты, поэтому потолок —
    # bank ДО дебета ставки (small_bank) + bet_total, но не полный
    # теоретический джекпот по всем 10 линиям.
    theoretical_max = slot_data.PAYTABLE["muscle"][5] * (bet_total // slot_engine.TOTAL_LINES) * slot_engine.TOTAL_LINES
    assert result["payout"] < theoretical_max
    assert result["payout"] <= small_bank + bet_total


@pytest.mark.asyncio
async def test_play_slots_scatter_freespin_wins_credit_to_balance(session, monkeypatch):
    """Репорты игроков (2026-07-28: "выиграл фриспин +200, баланс не
    изменился") заставили перепроверить, что деньги за фриспины реально
    доходят до UserBalance — раньше ни один тест не гонял play_slots
    ЦЕЛИКОМ (через реальный _settle/pay_from_bank) со scatter-триггерящей
    стартовой сеткой; test_play_slots_settles_and_is_idempotent и
    test_play_slots_payout_capped_to_bank форсируют сетки БЕЗ scatter, а
    тесты freespin_rounds выше вызывают evaluate_grid() напрямую, в обход
    settle-ядра. Результат: `total_payout` (стартовая линия + все бонусные
    раунды) доходит до баланса одним атомарным движением денег — баг,
    судя по всему, не в зачислении (см. коммит выше про фикс автопрокрута
    во фронте, miniapp/.../slots/+page.svelte)."""
    chat_id = -100900201
    user_id = 900201
    await _ensure_user(session, user_id)
    balance_before = await _fund(session, chat_id, user_id)
    await economy_service.credit_bank(
        session, chat_id, 10_000_000, kind="test_seed", ref_id="test_slot_scatter_seed"
    )
    await session.commit()

    initial_grid_symbols = [
        "keffiyeh", "sakaki", "keffiyeh", "sakaki", "sakaki",
        "sakaki", "sakaki", "sakaki", "sakaki", "keffiyeh",
        "sakaki", "sakaki", "sakaki", "sakaki", "sakaki",
    ]
    monkeypatch.setattr(casino_service, "_rng", _ForcedGridRng(initial_grid_symbols))
    # Every auto-played bonus spin: 5-in-a-row wild on all 15 cells (big win,
    # no scatter -> no retrigger, keeps the math simple).
    monkeypatch.setattr(slot_engine, "_rng", _ForcedGridRng(["muscle"] * 15))

    bet_total = 10 * slot_engine.TOTAL_LINES
    result = await casino_service.play_slots(session, chat_id, user_id, bet_total, "test_scatter_fs")

    assert result["outcome"]["scatter"] == 3
    assert result["outcome"]["freespins"] == slot_data.FREESPIN_TABLE[3]
    assert len(result["outcome"]["freespin_rounds"]) == slot_data.FREESPIN_TABLE[3]
    for round_ in result["outcome"]["freespin_rounds"]:
        assert round_["payout"] > 0  # every forced-wild bonus spin should win big

    balance_after = await _get_user_balance(session, chat_id, user_id)
    assert balance_after == balance_before - bet_total + result["payout"]


@pytest.mark.asyncio
async def test_play_slots_scatter_freespin_replay_returns_stored_outcome_without_recompute(
    session, monkeypatch
):
    """Аудит-регрессия (idempotency-replay): единственный существующий
    replay-тест (test_play_slots_settles_and_is_idempotent выше) форсирует
    НУЛЕВОЙ (без scatter) исход - доказывает идемпотентность только для
    тривиальной сдачи. test_play_slots_scatter_freespin_wins_credit_to_balance
    форсирует scatter/freespin-исход, но НЕ делает второго (replay) вызова.
    Здесь форсируем ту же scatter-триггерящую сдачу и делаем ВТОРОЙ вызов
    play_slots с тем же idem_key, проверяя: (а) второй результат побайтово
    идентичен первому; (б) счётчики вызовов ОБОИХ RNG-стабов (главная сетка
    через casino_service._rng + фриспин-цикл через slot_engine._rng) НЕ
    выросли после второго вызова - т.е. compute() реально не пересчитывался
    заново на replay, а не просто "совпал" по значению (существующий
    idempotent-тест использует forced_symbols ровно на 15 элементов, чей
    период совпадает с числом rng.choice-вызовов одного спина - здесь это
    исключено явным счётчиком, а не совпадением значений)."""
    chat_id = -100900301
    user_id = 900301
    await _ensure_user(session, user_id)
    await _fund(session, chat_id, user_id)
    await economy_service.credit_bank(
        session, chat_id, 10_000_000, kind="test_seed", ref_id="test_slot_scatter_replay_seed"
    )
    await session.commit()

    initial_grid_symbols = [
        "keffiyeh", "sakaki", "keffiyeh", "sakaki", "sakaki",
        "sakaki", "sakaki", "sakaki", "sakaki", "keffiyeh",
        "sakaki", "sakaki", "sakaki", "sakaki", "sakaki",
    ]
    main_rng = _ForcedGridRng(initial_grid_symbols)
    bonus_rng = _ForcedGridRng(["muscle"] * 15)
    monkeypatch.setattr(casino_service, "_rng", main_rng)
    monkeypatch.setattr(slot_engine, "_rng", bonus_rng)

    idem_key = "test_scatter_fs_replay"
    bet_total = 10 * slot_engine.TOTAL_LINES

    first = await casino_service.play_slots(session, chat_id, user_id, bet_total, idem_key)
    assert first["outcome"]["scatter"] == 3
    assert first["outcome"]["freespins"] == slot_data.FREESPIN_TABLE[3]
    assert len(first["outcome"]["freespin_rounds"]) == slot_data.FREESPIN_TABLE[3]
    balance_after_first = await _get_user_balance(session, chat_id, user_id)

    main_calls_after_first = main_rng._i
    bonus_calls_after_first = bonus_rng._i
    assert main_calls_after_first > 0
    assert bonus_calls_after_first > 0  # доказывает, что фриспин-цикл реально прогонялся

    second = await casino_service.play_slots(session, chat_id, user_id, bet_total, idem_key)

    # Ни один из RNG-стабов не тронут повторно -> compute() реально НЕ
    # вызывался заново на replay (а не просто совпал по значению).
    assert main_rng._i == main_calls_after_first
    assert bonus_rng._i == bonus_calls_after_first

    assert second["payout"] == first["payout"]
    assert second["outcome"] == first["outcome"]
    assert second["jackpot"] is None
    assert await _get_user_balance(session, chat_id, user_id) == balance_after_first

    games = (
        await session.execute(
            select(CasinoGame).where(CasinoGame.user_id == user_id, CasinoGame.idem_key == idem_key)
        )
    ).scalars().all()
    assert len(games) == 1


@pytest.mark.asyncio
async def test_play_slots_freespin_loop_error_blocks_naive_retry_without_fake_success(
    session, monkeypatch
):
    """Аудит-регрессия (rollback-on-error): если исключение возникает ВНУТРИ
    авто-прокрута фриспинов (`evaluate_grid`'s while-цикл на
    `slot_engine._rng` - самый RNG-насыщенный и потенциально самый "хрупкий"
    участок движка), уже ПОСЛЕ того как `_debit_stake` применил (но ещё не
    закоммитил) списание ставки, - исключение обязано пробрасываться наружу
    НЕПОДМЕНЁННЫМ (не проглатываться, не превращаться в фальшивый успех),
    CasinoGame-строка не должна появиться, а наивный повтор с ТЕМ ЖЕ idem_key
    (то, что реально сделал бы клиент после сетевого таймаута) обязан явно
    провалиться (`DuplicateRound`), а не списать ставку повторно и не выдать
    молчаливый фальшивый успех.

    Прим. про рамки теста: как и в
    test_settle_payout_failure_after_rng_does_not_fabricate_or_double_charge
    (tests/test_casino_service.py) - фикстура `session` (tests/conftest.py)
    кладёт сессию поверх УЖЕ открытой внешней транзакции
    (`join_transaction_mode="rollback_only"`), поэтому явный
    `session.rollback()` откатил бы ВЕСЬ тест целиком (включая сетап
    `_ensure_user`/`_fund`), а не только этот раунд - проверяем финансовый
    инвариант БЕЗ явного rollback, тем же способом, что и generic-тест
    pay_from_bank-сбоя, но здесь сбой форсируется именно во фриспин-цикле
    slot_engine (после ОДНОГО успешно сыгранного бонусного спина, не на
    первом же шаге), что для слотов не проверялось ни одним тестом."""
    chat_id = -100900302
    user_id = 900302
    monkeypatch.setattr(casino_service, "_last_slots_spin_at", {})
    await _ensure_user(session, user_id)
    balance_before = await _fund(session, chat_id, user_id)
    bank_before = await _get_bank_balance(session, chat_id)

    initial_grid_symbols = [
        "keffiyeh", "sakaki", "keffiyeh", "sakaki", "sakaki",
        "sakaki", "sakaki", "sakaki", "sakaki", "keffiyeh",
        "sakaki", "sakaki", "sakaki", "sakaki", "sakaki",
    ]
    monkeypatch.setattr(casino_service, "_rng", _ForcedGridRng(initial_grid_symbols))

    class _RaisingAfterNCallsRng:
        """RNG-стаб для slot_engine._rng (только фриспин-цикл): первые
        `raise_after` вызовов choice(...) отдают фиксированный non-scatter
        символ (один бонусный спин доигрывается успешно), затем кидает
        RuntimeError - форсирует сбой ПОСЛЕ хотя бы одного реально сыгранного
        авто-спина, не на первом же шаге (реалистичнее сценария из
        failure_scenario_ru аудита)."""

        def __init__(self, ok_symbol: str, raise_after: int):
            self._ok_symbol = ok_symbol
            self._raise_after = raise_after
            self._calls = 0

        def choice(self, seq):
            self._calls += 1
            if self._calls > self._raise_after:
                raise RuntimeError("симулированный сбой во фриспин-цикле")
            return self._ok_symbol

    # 15 rng.choice-вызовов = ровно один полный (без scatter) бонусный спин;
    # 16-й вызов (начало ВТОРОГО бонусного спина) кидает исключение.
    monkeypatch.setattr(slot_engine, "_rng", _RaisingAfterNCallsRng("sakaki", raise_after=15))

    bet_total = 10 * slot_engine.TOTAL_LINES
    idem_key = "test_slot_fs_error_blocks_retry"

    with pytest.raises(RuntimeError):
        await casino_service.play_slots(session, chat_id, user_id, bet_total, idem_key)

    # Ставка уже была списана _debit_stake ДО сбоя во фриспин-цикле - деньги
    # "в подвешенном" состоянии внутри текущей (пока не завершённой)
    # транзакции, ждут исхода раунда (bank += bet_total, баланс игрока -= bet_total).
    assert await _get_user_balance(session, chat_id, user_id) == balance_before - bet_total
    assert await _get_bank_balance(session, chat_id) == bank_before + bet_total

    # Раунд НЕ записан как успешный итог - исключение не было тихо проглочено.
    games = (
        await session.execute(
            select(CasinoGame).where(CasinoGame.user_id == user_id, CasinoGame.idem_key == idem_key)
        )
    ).scalars().all()
    assert games == []

    # Наивный повтор с тем же idem_key (клиент после сетевого таймаута) не
    # должен списать ставку повторно и не должен тихо "успешно" завершиться -
    # ref_id `_debit_stake` уже применён, а строки CasinoGame ещё нет, поэтому
    # ожидается явный DuplicateRound, а не задвоенное списание/фальшивый успех.
    monkeypatch.setattr(casino_service, "_last_slots_spin_at", {})
    with pytest.raises(casino_service.DuplicateRound):
        await casino_service.play_slots(session, chat_id, user_id, bet_total, idem_key)

    # Баланс/банк не изменились повторно (никакого двойного списания).
    assert await _get_user_balance(session, chat_id, user_id) == balance_before - bet_total
    assert await _get_bank_balance(session, chat_id) == bank_before + bet_total
