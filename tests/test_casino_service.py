"""Интеграционные тесты casino_service против живого Postgres (фикстура
`session` из tests/conftest.py — транзакция-на-тест). Доказывают фундамент
казино (04.1-01): идемпотентность раунда по `idem_key` (повтор не двигает
деньги повторно и возвращает тот же сохранённый исход), D-06 (выигрыш
урезается до остатка chat_bank, банк никогда не уходит в минус), D-04/D-05
(лимиты минимальной/максимальной ставки — до любого движения денег), D-03
(точные формулы: коинфлип 1.98x, дайс mult=(1-0.02)/win_prob, рулетка
европейская number 36x / color-parity-half 2x / dozen 3x).

Все исходы форсируются через RNG-seam `casino_service._rng`
(`secrets.SystemRandom`-совместимый объект, monkeypatched в тестах) — никогда
не проверяем реальную случайность.

casino_service сам делает session.commit() там, где это описано в его
контракте — совместимо с фикстурой session благодаря join-savepoint режиму
SQLAlchemy 2.0 (тот же паттерн уже проверен в test_markets_service.py).
"""

from __future__ import annotations

import json
from datetime import datetime
from datetime import timedelta
from decimal import Decimal
from fractions import Fraction

import pytest
from sqlalchemy import select
from sqlalchemy import text

from bot.config import settings
from bot.services import blackjack_engine
from bot.services import casino_service
from bot.services import crash_engine
from bot.services import economy_service
from bot.services import teto_slot_engine
from common.models.casino_game import CasinoGame
from common.models.chat_bank import ChatBank
from common.models.user import User
from common.models.user_balance import UserBalance


async def _ensure_user(session, user_id: int, first_name: str = "Тест") -> None:
    session.add(User(id=user_id, first_name=first_name))
    await session.flush()


async def _fund(session, chat_id: int, user_id: int) -> int:
    """Заводит кошелёк (стартовый бонус economy_start_bonus) и коммитит."""
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


def _min_valid_teto_bet() -> int:
    """Наименьшая ставка тето-слота, одновременно кратная
    `teto_slot_engine.TOTAL_LINES` и не меньше `settings.casino_min_bet` — не
    хардкодим конкретное число (напр. "30"), чтобы тесты не рассинхронизировались
    молча, если один из двух параметров (число линий MVP-набора или минимальная
    ставка казино) когда-нибудь изменится."""
    lines = teto_slot_engine.TOTAL_LINES
    bet_per_line = -(-settings.casino_min_bet // lines)  # ceil division без float
    return bet_per_line * lines


class _ForcedRng:
    """Тестовый RNG-стаб, monkeypatched вместо `casino_service._rng`.

    Позволяет форсировать детерминированный исход коинфлипа/кости/рулетки
    вместо реальной случайности `secrets.SystemRandom`.
    """

    def __init__(self, choice_value=None, randint_value: int | None = None):
        self._choice_value = choice_value
        self._randint_value = randint_value

    def choice(self, seq):
        if self._choice_value is not None:
            return self._choice_value
        return seq[0]

    def randint(self, a: int, b: int) -> int:
        if self._randint_value is not None:
            return self._randint_value
        return a


class _FixedDeckRng:
    """Тестовый RNG-стаб для форсированной колоды блэкджека (та же форма, что
    `tests/test_blackjack_service.py::_FixedDeckRng`) — `shuffle(deck)`
    переставляет 52-карточную колоду так, чтобы `deck.pop()` (берёт с КОНЦА)
    отдавал карты строго в порядке `pop_sequence`."""

    def __init__(self, pop_sequence: list[str]):
        self._pop_sequence = pop_sequence

    def shuffle(self, deck: list[str]) -> None:
        remaining = list(deck)
        for card in self._pop_sequence:
            remaining.remove(card)
        deck[:] = remaining + list(reversed(self._pop_sequence))


# --- Лимиты ставок (D-04/D-05) -----------------------------------------------


@pytest.mark.asyncio
async def test_bet_below_min_rejected(session):
    chat_id = -100900001
    user_id = 900001
    await _ensure_user(session, user_id)
    balance_before = await _fund(session, chat_id, user_id)

    with pytest.raises(casino_service.InvalidBet):
        await casino_service.play_coinflip(
            session, chat_id, user_id, settings.casino_min_bet - 1, "heads", "test_min_bet_reject"
        )

    assert await _get_user_balance(session, chat_id, user_id) == balance_before
    games = (
        await session.execute(select(CasinoGame).where(CasinoGame.user_id == user_id))
    ).scalars().all()
    assert games == []


@pytest.mark.asyncio
async def test_bet_above_balance_rejected(session):
    chat_id = -100900002
    user_id = 900002
    await _ensure_user(session, user_id)
    balance_before = await _fund(session, chat_id, user_id)

    with pytest.raises((casino_service.InvalidBet, economy_service.InsufficientFunds)):
        await casino_service.play_coinflip(
            session, chat_id, user_id, balance_before + 1000, "heads", "test_above_balance_reject"
        )

    assert await _get_user_balance(session, chat_id, user_id) == balance_before
    games = (
        await session.execute(select(CasinoGame).where(CasinoGame.user_id == user_id))
    ).scalars().all()
    assert games == []


# --- Коинфлип (D-03: 1.98x) --------------------------------------------------


@pytest.mark.asyncio
async def test_coinflip_win_pays_1_98x(session, monkeypatch):
    chat_id = -100900003
    user_id = 900003
    await _ensure_user(session, user_id)
    balance_before = await _fund(session, chat_id, user_id)
    # банк надо наполнить, иначе выигрыш урежется D-06 — не то, что здесь тестируем
    await economy_service.credit_bank(
        session, chat_id, 100_000, kind="test_seed", ref_id="test_coinflip_seed_bank"
    )
    await session.commit()

    monkeypatch.setattr(casino_service, "_rng", _ForcedRng(choice_value="heads"))

    bet = 100
    result = await casino_service.play_coinflip(
        session, chat_id, user_id, bet, "heads", "test_coinflip_win"
    )

    expected_payout = int(bet * casino_service.COINFLIP_MULT)
    assert result["payout"] == expected_payout
    assert await _get_user_balance(session, chat_id, user_id) == balance_before - bet + expected_payout

    game = await _get_casino_game(session, user_id, "test_coinflip_win")
    assert game.payout == expected_payout
    assert game.bet == bet
    assert game.game == "coinflip"


@pytest.mark.asyncio
async def test_coinflip_loss_keeps_stake_in_bank(session, monkeypatch):
    chat_id = -100900004
    user_id = 900004
    await _ensure_user(session, user_id)
    balance_before = await _fund(session, chat_id, user_id)
    bank_before = await _get_bank_balance(session, chat_id)

    monkeypatch.setattr(casino_service, "_rng", _ForcedRng(choice_value="tails"))

    bet = 50
    result = await casino_service.play_coinflip(
        session, chat_id, user_id, bet, "heads", "test_coinflip_loss"
    )

    assert result["payout"] == 0
    assert await _get_user_balance(session, chat_id, user_id) == balance_before - bet
    assert await _get_bank_balance(session, chat_id) == bank_before + bet


# --- Дайс (D-03: mult=(1-0.02)/win_prob) -------------------------------------


@pytest.mark.asyncio
async def test_dice_multiplier_matches_formula(session, monkeypatch):
    chat_id = -100900005
    user_id = 900005
    await _ensure_user(session, user_id)
    balance_before = await _fund(session, chat_id, user_id)
    await economy_service.credit_bank(
        session, chat_id, 100_000, kind="test_seed", ref_id="test_dice_seed_bank"
    )
    await session.commit()

    target = 50
    direction = "under"
    # r < target => under wins; форсируем r=1 (гарантированный under-win)
    monkeypatch.setattr(casino_service, "_rng", _ForcedRng(randint_value=1))

    bet = 100
    result = await casino_service.play_dice(
        session, chat_id, user_id, bet, target, direction, "test_dice_win"
    )

    # Fraction — независимо от продакшен-кода, а не тот же float, что и
    # implementation (аудит-регрессия 2026-08-06: обе стороны раньше несли
    # ОДНУ и ту же float-погрешность округления и потому не могли поймать её).
    exact = bet * (1 - Fraction(2, 100)) / Fraction(target - 1, 100)
    expected_payout = exact.numerator // exact.denominator
    assert result["payout"] == expected_payout
    assert await _get_user_balance(session, chat_id, user_id) == balance_before - bet + expected_payout


@pytest.mark.asyncio
async def test_dice_payout_matches_exact_rational_formula_not_lossy_float(session, monkeypatch):
    """Аудит-регрессия 2026-08-06: `int(bet * (1 - DICE_HOUSE_EDGE) /
    win_prob)` теряет точность на float DICE_HOUSE_EDGE=0.02 и float
    win_prob=(target-1)/100 — на target=2, bet=14 старая формула давала 1371
    вместо точного рационального floor 1372 (проверено экзостивным сравнением
    с `fractions.Fraction` по всем target/bet — ~3.5% пар расходились, в обе
    стороны, не системная скидка в пользу банка). Форсируем roll=1
    (гарантированный under-win при любом target>=2)."""
    chat_id = -100900024
    user_id = 900024
    await _ensure_user(session, user_id)
    balance_before = await _fund(session, chat_id, user_id)
    await economy_service.credit_bank(
        session, chat_id, 1_000_000, kind="test_seed", ref_id="test_dice_exact_seed_bank"
    )
    await session.commit()

    target = 2
    bet = 14
    monkeypatch.setattr(casino_service, "_rng", _ForcedRng(randint_value=1))

    result = await casino_service.play_dice(
        session, chat_id, user_id, bet, target, "under", "test_dice_exact_win"
    )

    old_lossy_formula_payout = int(bet * (1 - casino_service.DICE_HOUSE_EDGE) / ((target - 1) / 100))
    exact = bet * (1 - Fraction(2, 100)) / Fraction(target - 1, 100)
    exact_payout = exact.numerator // exact.denominator

    assert old_lossy_formula_payout == 1371
    assert exact_payout == 1372
    assert result["payout"] == exact_payout
    assert result["payout"] != old_lossy_formula_payout
    assert await _get_user_balance(session, chat_id, user_id) == balance_before - bet + exact_payout


@pytest.mark.asyncio
async def test_dice_fair_payout_matches_exact_rational_formula_exhaustively():
    """`casino_service.dice_fair_payout` — независимо проверено против
    `fractions.Fraction` (не переиспользует ту же формулу изнутри) по ВСЕМ
    98 target x 2 direction x репрезентативному набору ставок — включая
    маленькие (1..50) и большие (до 2**31-1) значения bet."""
    bets = list(range(1, 51)) + [999, 100_000, 999_999_999, 2**31 - 1]
    for target in range(2, 100):
        for direction, denom in (("under", target - 1), ("over", 100 - target)):
            win_prob = Fraction(denom, 100)
            for bet in bets:
                exact = bet * (1 - Fraction(2, 100)) / win_prob
                expected = exact.numerator // exact.denominator
                assert casino_service.dice_fair_payout(bet, target, direction) == expected


# --- Рулетка (D-03: number 36x / color-parity-half 2x / dozen 3x) -----------


@pytest.mark.asyncio
async def test_roulette_number_pays_36x(session, monkeypatch):
    chat_id = -100900006
    user_id = 900006
    await _ensure_user(session, user_id)
    balance_before = await _fund(session, chat_id, user_id)
    await economy_service.credit_bank(
        session, chat_id, 100_000, kind="test_seed", ref_id="test_roulette_number_seed_bank"
    )
    await session.commit()

    monkeypatch.setattr(casino_service, "_rng", _ForcedRng(randint_value=7))

    bet = 10
    result = await casino_service.play_roulette(
        session, chat_id, user_id, bet, "number", 7, "test_roulette_number_win"
    )

    expected_payout = int(bet * casino_service.ROULETTE_NUMBER_MULT)
    assert result["payout"] == expected_payout
    assert await _get_user_balance(session, chat_id, user_id) == balance_before - bet + expected_payout


@pytest.mark.asyncio
async def test_roulette_color_pays_2x(session, monkeypatch):
    chat_id = -100900007
    user_id = 900007
    await _ensure_user(session, user_id)
    balance_before = await _fund(session, chat_id, user_id)
    await economy_service.credit_bank(
        session, chat_id, 100_000, kind="test_seed", ref_id="test_roulette_color_seed_bank"
    )
    await session.commit()

    # 1 — красное число в европейской рулетке
    monkeypatch.setattr(casino_service, "_rng", _ForcedRng(randint_value=1))

    bet = 10
    result = await casino_service.play_roulette(
        session, chat_id, user_id, bet, "color", "red", "test_roulette_color_win"
    )

    expected_payout = int(bet * casino_service.ROULETTE_EVEN_MULT)
    assert result["payout"] == expected_payout
    assert await _get_user_balance(session, chat_id, user_id) == balance_before - bet + expected_payout


@pytest.mark.asyncio
async def test_roulette_dozen_pays_3x(session, monkeypatch):
    chat_id = -100900008
    user_id = 900008
    await _ensure_user(session, user_id)
    balance_before = await _fund(session, chat_id, user_id)
    await economy_service.credit_bank(
        session, chat_id, 100_000, kind="test_seed", ref_id="test_roulette_dozen_seed_bank"
    )
    await session.commit()

    # 5 попадает в первую дюжину (1-12)
    monkeypatch.setattr(casino_service, "_rng", _ForcedRng(randint_value=5))

    bet = 10
    result = await casino_service.play_roulette(
        session, chat_id, user_id, bet, "dozen", 1, "test_roulette_dozen_win"
    )

    expected_payout = int(bet * casino_service.ROULETTE_DOZEN_MULT)
    assert result["payout"] == expected_payout
    assert await _get_user_balance(session, chat_id, user_id) == balance_before - bet + expected_payout


# --- Валидация bet_value рулетки (WR-06) --------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bet_type,bet_value",
    [
        ("number", 999),
        ("number", -1),
        ("number", "seven"),
        ("color", "purple"),
        ("parity", "maybe"),
        ("half", "middle"),
        ("dozen", 4),
    ],
)
async def test_roulette_invalid_bet_value_raises(session, bet_type, bet_value):
    """WR-06 (04.1-REVIEW) regression: bet_value вне легального домена для
    своего bet_type раньше молча принимался как всегда проигрывающая ставка
    (клиентский баг маскировался под обычный проигрыш) — теперь явный
    InvalidBet ДО списания денег."""
    chat_id = -100900011
    user_id = 900011
    await _ensure_user(session, user_id)
    balance_before = await _fund(session, chat_id, user_id)

    with pytest.raises(casino_service.InvalidBet):
        await casino_service.play_roulette(
            session, chat_id, user_id, 10, bet_type, bet_value, f"test_roulette_invalid_{bet_type}"
        )

    assert await _get_user_balance(session, chat_id, user_id) == balance_before


# --- Идемпотентность replay --------------------------------------------------


@pytest.mark.asyncio
async def test_idempotent_replay_same_idem_key(session, monkeypatch):
    chat_id = -100900009
    user_id = 900009
    await _ensure_user(session, user_id)
    await _fund(session, chat_id, user_id)
    await economy_service.credit_bank(
        session, chat_id, 100_000, kind="test_seed", ref_id="test_idempotent_seed_bank"
    )
    await session.commit()

    monkeypatch.setattr(casino_service, "_rng", _ForcedRng(choice_value="heads"))

    idem_key = "test_idempotent_replay"
    bet = 100
    first = await casino_service.play_coinflip(session, chat_id, user_id, bet, "heads", idem_key)

    balance_after_first = await _get_user_balance(session, chat_id, user_id)

    second = await casino_service.play_coinflip(session, chat_id, user_id, bet, "heads", idem_key)

    assert second["payout"] == first["payout"]
    assert second["outcome"] == first["outcome"]

    # деньги не двинулись повторно
    assert await _get_user_balance(session, chat_id, user_id) == balance_after_first

    games = (
        await session.execute(
            select(CasinoGame).where(CasinoGame.user_id == user_id, CasinoGame.idem_key == idem_key)
        )
    ).scalars().all()
    assert len(games) == 1


# --- Bank cap (D-06) ----------------------------------------------------------


@pytest.mark.asyncio
async def test_payout_capped_to_bank_balance(session, monkeypatch):
    chat_id = -100900010
    user_id = 900010
    await _ensure_user(session, user_id)
    await _fund(session, chat_id, user_id)

    # Почти пустой банк — выигрыш точно превысит его остаток.
    small_bank = 5
    await economy_service.credit_bank(
        session, chat_id, small_bank, kind="test_seed", ref_id="test_bank_cap_seed_bank"
    )
    await session.commit()

    monkeypatch.setattr(casino_service, "_rng", _ForcedRng(choice_value="heads"))

    bet = 1000
    result = await casino_service.play_coinflip(
        session, chat_id, user_id, bet, "heads", "test_bank_cap_win"
    )

    # Полный payout был бы int(bet*1.98) = 1980, но банк после дебета ставки
    # (bet зачислен в банк ДО выплаты) не может позволить себе весь выигрыш.
    bank_balance_after = await _get_bank_balance(session, chat_id)
    assert bank_balance_after >= 0

    game = await _get_casino_game(session, user_id, "test_bank_cap_win")
    assert result["payout"] == game.payout
    # выигрыш урезан — не полная сумма 1980
    assert result["payout"] < int(bet * casino_service.COINFLIP_MULT)


# --- Троттлинг слотов (анти-абьюз авто-спина) --------------------------------


@pytest.mark.asyncio
async def test_slots_second_spin_too_soon_raises_rate_limited(session, monkeypatch):
    chat_id = -100900013
    user_id = 900013
    await _ensure_user(session, user_id)
    await _fund(session, chat_id, user_id)
    monkeypatch.setattr(casino_service, "_last_slots_spin_at", {})

    first = await casino_service.play_slots(session, chat_id, user_id, 100, "test_throttle_spin_1")
    balance_after_first = await _get_user_balance(session, chat_id, user_id)

    with pytest.raises(casino_service.RateLimited):
        await casino_service.play_slots(session, chat_id, user_id, 100, "test_throttle_spin_2")

    # второй (отклонённый) спин не двинул деньги и не создал свою запись
    assert await _get_user_balance(session, chat_id, user_id) == balance_after_first
    games = (
        await session.execute(
            select(CasinoGame).where(
                CasinoGame.user_id == user_id, CasinoGame.idem_key == "test_throttle_spin_2"
            )
        )
    ).scalars().all()
    assert games == []
    assert first["game"] == "slots"


@pytest.mark.asyncio
async def test_slots_spin_allowed_once_interval_elapsed(session, monkeypatch):
    chat_id = -100900014
    user_id = 900014
    await _ensure_user(session, user_id)
    await _fund(session, chat_id, user_id)
    monkeypatch.setattr(casino_service, "_last_slots_spin_at", {})

    await casino_service.play_slots(session, chat_id, user_id, 100, "test_throttle_elapsed_1")

    # Симулируем, что интервал уже прошёл — сдвигаем сохранённую отметку
    # времени в прошлое, не подставляя реальный sleep() в тест.
    casino_service._last_slots_spin_at[user_id] -= (
        casino_service.settings.casino_spin_min_interval_ms / 1000 + 1
    )

    second = await casino_service.play_slots(session, chat_id, user_id, 100, "test_throttle_elapsed_2")
    assert second["game"] == "slots"


# --- Тето Брейнрот: Дрель-Хант (money-интеграция play_teto_slots) -----------
#
# Движок (`teto_slot_engine.play_one_spin`) уже полностью протестирован своими
# собственными тестами (`tests/test_teto_slot_engine.py`) — мегаблоки/тумбл/
# Дрель-Хант/лестница/хардкапы там НЕ переповторяются. Здесь — ТОЛЬКО слой
# money/DB/settle-плюмбинга, тот же принцип, что у остальных тестов этого
# файла для play_slots.


@pytest.mark.asyncio
async def test_teto_slots_idempotent_replay_same_idem_key(session, monkeypatch):
    """Повторный вызов с ТЕМ ЖЕ idem_key не должен списывать ставку дважды и
    обязан вернуть строго ОДНУ строку CasinoGame (форма
    `test_idempotent_replay_same_idem_key` выше, только для teto-слота)."""
    chat_id = -100900040
    user_id = 900040
    await _ensure_user(session, user_id)
    balance_before = await _fund(session, chat_id, user_id)
    await economy_service.credit_bank(
        session, chat_id, 100_000, kind="test_seed", ref_id="test_teto_idempotent_seed_bank"
    )
    await session.commit()
    monkeypatch.setattr(casino_service, "_last_slots_spin_at", {})

    idem_key = "test_teto_idempotent_replay"
    bet = _min_valid_teto_bet()
    first = await casino_service.play_teto_slots(session, chat_id, user_id, bet, idem_key)
    balance_after_first = await _get_user_balance(session, chat_id, user_id)

    # Явная проверка дельты баланса (не только "второй вызов вернул то же
    # самое"): ровно один дебет ставки + один кредит выплаты первого вызова.
    assert balance_after_first == balance_before - bet + first["payout"]

    second = await casino_service.play_teto_slots(session, chat_id, user_id, bet, idem_key)

    assert second["payout"] == first["payout"]
    assert second["outcome"] == first["outcome"]
    assert second["game"] == "teto_slots"

    # деньги не двинулись повторно — баланс идентичен состоянию ПОСЛЕ первого
    # (не-replay) вызова, а не balance_before (та ставка реально списывается)
    assert await _get_user_balance(session, chat_id, user_id) == balance_after_first

    games = (
        await session.execute(
            select(CasinoGame).where(CasinoGame.user_id == user_id, CasinoGame.idem_key == idem_key)
        )
    ).scalars().all()
    assert len(games) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "user_id,bad_bet",
    [(900041, 0), (900042, -10)],
)
async def test_teto_slots_non_positive_bet_rejected(session, monkeypatch, user_id, bad_bet):
    """bet=0/отрицательный — InvalidBet ДО любого движения денег, деньги не
    двигаются, строка CasinoGame не создаётся (форма `test_bet_below_min_rejected`)."""
    chat_id = -100900041
    await _ensure_user(session, user_id)
    balance_before = await _fund(session, chat_id, user_id)
    monkeypatch.setattr(casino_service, "_last_slots_spin_at", {})

    with pytest.raises(casino_service.InvalidBet):
        await casino_service.play_teto_slots(
            session, chat_id, user_id, bad_bet, f"test_teto_bad_bet_{user_id}"
        )

    assert await _get_user_balance(session, chat_id, user_id) == balance_before
    games = (
        await session.execute(select(CasinoGame).where(CasinoGame.user_id == user_id))
    ).scalars().all()
    assert games == []


@pytest.mark.asyncio
async def test_teto_slots_bet_not_multiple_of_lines_rejected(session, monkeypatch):
    """Положительная ставка, НЕ кратная `teto_slot_engine.TOTAL_LINES` —
    та же валидация, что и у Azumanga `play_slots`, только на другом
    делителе (число линий MVP-набора `TETO_PAYLINES`, см. `teto_tumble.py`)."""
    chat_id = -100900043
    user_id = 900043
    await _ensure_user(session, user_id)
    balance_before = await _fund(session, chat_id, user_id)
    monkeypatch.setattr(casino_service, "_last_slots_spin_at", {})

    bet = _min_valid_teto_bet() + 1
    assert bet % teto_slot_engine.TOTAL_LINES != 0

    with pytest.raises(casino_service.InvalidBet):
        await casino_service.play_teto_slots(session, chat_id, user_id, bet, "test_teto_bad_bet_not_multiple")

    assert await _get_user_balance(session, chat_id, user_id) == balance_before
    games = (
        await session.execute(select(CasinoGame).where(CasinoGame.user_id == user_id))
    ).scalars().all()
    assert games == []


@pytest.mark.asyncio
async def test_teto_slots_second_spin_too_soon_raises_rate_limited(session, monkeypatch):
    """Троттлинг-дикт/функция ОБЩИЕ с Azumanga `play_slots` (см. обновлённый
    докстринг `_check_slots_throttle`) — та же форма, что
    `test_slots_second_spin_too_soon_raises_rate_limited`, только для
    teto-слота, доказывает, что переиспользование работает и на новой игре."""
    chat_id = -100900044
    user_id = 900044
    await _ensure_user(session, user_id)
    await _fund(session, chat_id, user_id)
    monkeypatch.setattr(casino_service, "_last_slots_spin_at", {})

    bet = _min_valid_teto_bet()
    first = await casino_service.play_teto_slots(session, chat_id, user_id, bet, "test_teto_throttle_1")
    balance_after_first = await _get_user_balance(session, chat_id, user_id)

    with pytest.raises(casino_service.RateLimited):
        await casino_service.play_teto_slots(session, chat_id, user_id, bet, "test_teto_throttle_2")

    assert await _get_user_balance(session, chat_id, user_id) == balance_after_first
    games = (
        await session.execute(
            select(CasinoGame).where(
                CasinoGame.user_id == user_id, CasinoGame.idem_key == "test_teto_throttle_2"
            )
        )
    ).scalars().all()
    assert games == []
    assert first["game"] == "teto_slots"


@pytest.mark.asyncio
async def test_teto_slots_throttle_shared_across_slot_game_types(session, monkeypatch):
    """Демонстрирует буквальное переиспользование ОДНОГО И ТОГО ЖЕ
    троттлинг-дикта между Azumanga и Тето: спин `play_slots`, сразу за ним
    (без паузы) спин `play_teto_slots` ТЕМ ЖЕ user_id — второй обязан
    словить RateLimited, хотя это ДРУГАЯ игра, потому что троттлинг-ключ —
    ТОЛЬКО user_id, не (game, user_id) — см. тех-задание про переиспользование
    `_check_slots_throttle`/`_last_slots_spin_at` без форка второго дикта."""
    chat_id = -100900045
    user_id = 900045
    await _ensure_user(session, user_id)
    await _fund(session, chat_id, user_id)
    monkeypatch.setattr(casino_service, "_last_slots_spin_at", {})

    await casino_service.play_slots(session, chat_id, user_id, 100, "test_teto_cross_throttle_azumanga")

    bet = _min_valid_teto_bet()
    with pytest.raises(casino_service.RateLimited):
        await casino_service.play_teto_slots(session, chat_id, user_id, bet, "test_teto_cross_throttle_teto")


@pytest.mark.asyncio
async def test_teto_slots_replay_of_settled_idem_key_not_throttled(session, monkeypatch):
    """Гард `is_new_spin` (см. докстринг `_check_slots_throttle`): replay уже
    settled idem_key НЕ должен ловить RateLimited, даже если вызван сразу же
    после первого спина — троттлинг применяется ТОЛЬКО к подтверждённо новым
    раундам, не к повтору уже обработанного idem_key."""
    chat_id = -100900046
    user_id = 900046
    await _ensure_user(session, user_id)
    await _fund(session, chat_id, user_id)
    monkeypatch.setattr(casino_service, "_last_slots_spin_at", {})

    idem_key = "test_teto_replay_not_throttled"
    bet = _min_valid_teto_bet()
    first = await casino_service.play_teto_slots(session, chat_id, user_id, bet, idem_key)

    # Мгновенный повтор ТЕМ ЖЕ idem_key сразу следом — не новый спин
    # (_find_existing находит уже settled строку), поэтому не должен словить
    # RateLimited, несмотря на то, что реальный интервал между вызовами
    # заведомо меньше casino_spin_min_interval_ms.
    second = await casino_service.play_teto_slots(session, chat_id, user_id, bet, idem_key)

    assert second == first


@pytest.mark.asyncio
async def test_teto_slots_real_spin_outcome_round_trips_through_json(session, monkeypatch):
    """Task 2 (`teto_slot_engine.serialize_spin_result`) доказывается ЗДЕСЬ
    end-to-end: реальный (не форсированный) `_rng` — та же философия, что и
    структурные тесты `test_api_games.py` для Azumanga-слота (форсировать
    детерминированный исход через весь мегаблок+тумбл+Дрель-Хант+лестница
    RNG-chain было бы хрупко и не нужно; идемпотентность/bank-capping уже
    доказаны generic-тестами казино выше и не переповторяются здесь через
    форсированный тето-выигрыш).

    Троттлинг здесь намеренно отключён (`_check_slots_throttle` — no-op):
    этот тест — не про троттлинг (тот покрыт отдельными тестами выше), а
    про сериализацию, для которой нужно прогнать НЕСКОЛЬКО настоящих спинов
    подряд без искусственных задержек, пытаясь застать (не гарантированно)
    ветку с фриспинами — именно она добавляет ЕЩЁ один уровень вложенных
    MegaBlock-списков (`fs_round_records[i]["blocks"]`), самый ценный кейс
    регрессии для этой функции."""
    chat_id = -100900047
    user_id = 900047
    await _ensure_user(session, user_id)
    await _fund(session, chat_id, user_id)
    await economy_service.credit_bank(
        session, chat_id, 1_000_000, kind="test_seed", ref_id="test_teto_real_spin_seed_bank"
    )
    await session.commit()
    monkeypatch.setattr(casino_service, "_check_slots_throttle", lambda user_id: None)

    bet = _min_valid_teto_bet()

    first_idem = "test_teto_real_spin_0"
    result = await casino_service.play_teto_slots(session, chat_id, user_id, bet, first_idem)

    assert result["game"] == "teto_slots"
    assert isinstance(result["payout"], int)
    assert result["payout"] >= 0

    # Сама суть проверки: play_one_spin возвращает dataclass-инстансы
    # (MegaBlock/LadderState с полем-set), которые json.dumps НЕ умеет
    # сериализовать напрямую — если serialize_spin_result пропустила хотя бы
    # одно вложенное место (см. её докстринг), этот dumps упал бы с TypeError
    # на РЕАЛЬНОМ спине, не только в теории.
    round_tripped = json.loads(json.dumps(result["outcome"]))
    assert round_tripped == result["outcome"]

    game = await _get_casino_game(session, user_id, first_idem)
    assert game.game == "teto_slots"

    # Ещё несколько настоящих спинов подряд, пытаясь застать ветку с
    # фриспинами хотя бы раз — не гарантируем успех (реальный RNG), round-trip
    # выше уже безусловно доказал корректность сериализации на КАКОМ-ТО
    # реальном спине, это — просто повышение уверенности покрытия.
    hit_freespins = result["outcome"]["freespins_played"] > 0
    for i in range(1, 40):
        idem_key = f"test_teto_real_spin_{i}"
        result = await casino_service.play_teto_slots(session, chat_id, user_id, bet, idem_key)
        json.loads(json.dumps(result["outcome"]))  # тот же safety-net на каждой попытке
        if result["outcome"]["freespins_played"] > 0:
            hit_freespins = True
            break

    if not hit_freespins:
        logger_msg = "не удалось застать ветку с фриспинами за 40 реальных спинов — не блокирующе"
        print(f"\n[test_teto_slots_real_spin_outcome_round_trips_through_json] {logger_msg}")


# --- Тето: трейс анимации (animation_sink) — побочный канал, не outcome ------
#
# Схема самого трейса (5 op'ов, сегментация по раундам, пределы размера) уже
# покрыта `tests/test_teto_slot_engine.py` и здесь НЕ переповторяется. Тут —
# только плюмбинг money-слоя: кто заполняет sink, когда он обязан остаться
# пустым, и главное — что трейс НИКОГДА не оказывается в JSONB-колонке
# `CasinoGame.outcome`.


@pytest.mark.asyncio
async def test_teto_slots_animation_sink_filled_on_fresh_spin_and_empty_on_replay(session, monkeypatch):
    """Замок сразу на два свойства, ради которых `animation_sink` сделан
    OUT-параметром, а не новым ключом возвращаемого dict'а:

      1. На СВЕЖЕМ спине sink заполнен полноценным конвертом анимации.
      2. На replay ТОГО ЖЕ `idem_key` sink остаётся ПУСТЫМ — `_settle`
         возвращает сохранённый исход, не вызывая `compute()` вовсе, поэтому
         трейса физически нет. Синтезировать его нельзя: повторный
         `play_one_spin` съел бы свежий `_rng` и показал бы игроку анимацию
         спина, которого не было, с выигрышем, не совпадающим с изменением
         баланса (см. докстринг `play_teto_slots`).
      3. И при этом ВОЗВРАЩАЕМОЕ значение обоих вызовов идентично (`second ==
         first`) — то самое свойство, которое проверяет
         `test_teto_slots_replay_of_settled_idem_key_not_throttled` выше и
         которое сломал бы ключ `animation` в возврате (dict на свежем спине,
         None на replay)."""
    chat_id = -100900048
    user_id = 900048
    await _ensure_user(session, user_id)
    await _fund(session, chat_id, user_id)
    await economy_service.credit_bank(
        session, chat_id, 1_000_000, kind="test_seed", ref_id="test_teto_anim_sink_seed_bank"
    )
    await session.commit()
    monkeypatch.setattr(casino_service, "_last_slots_spin_at", {})

    idem_key = "test_teto_animation_sink_replay"
    bet = _min_valid_teto_bet()

    sink: dict = {}
    first = await casino_service.play_teto_slots(
        session, chat_id, user_id, bet, idem_key, animation_sink=sink
    )

    assert sink, "на свежем спине sink обязан быть заполнен"
    assert sink["version"] == teto_slot_engine.TRACE_SCHEMA_VERSION
    assert sink["ops"], "конверт без op'ов бесполезен фронту"
    assert sink["ops"][0]["op"] == "fill", "первый кадр спина — первичное заполнение доски"
    assert sink["ops"][-1]["op"] == "round_end"
    assert sink["truncated"] is False, "обычный спин не должен усекаться (см. TRACE_MAX_OPS)"
    assert sink["rounds_recorded"] == sink["rounds_total"] == 1 + first["outcome"]["freespins_played"]

    # Деньги в конверте — то, ради чего `serialize_animation` вызывается ПОСЛЕ
    # `_settle`, а не внутри `compute()`: до капа банком (D-06) фактическая
    # выплата попросту неизвестна. `payout_paid` обязан быть тем же числом,
    # что и `payout` возврата (== изменение баланса игрока), иначе счётчик на
    # экране досчитает до суммы, которой игрок не получил.
    assert sink["payout_paid"] == first["payout"]
    assert sink["payout_engine_total"] == first["outcome"]["total_payout"]
    # Банк здесь заведомо богатый (1 000 000 выше) — капа быть не может.
    assert sink["bank_capped"] is False
    assert first["payout"] == first["outcome"]["total_payout"]

    # Возврат — РОВНО те же четыре ключа, что и до появления анимации.
    assert set(first) == {"game", "bet", "payout", "outcome"}

    replay_sink: dict = {"мусор_от_предыдущего_запроса": 1}
    replay_sink.clear()
    second = await casino_service.play_teto_slots(
        session, chat_id, user_id, bet, idem_key, animation_sink=replay_sink
    )

    assert replay_sink == {}, "на replay трейса нет — синтезировать его было бы ложью про спин"
    assert second == first, "возврат обязан быть идентичен между свежим спином и replay"

    # Без sink'а вообще (путь любого вызывающего, которому анимация не нужна —
    # напр. бот-команда) — то же поведение, никаких новых ключей.
    third = await casino_service.play_teto_slots(session, chat_id, user_id, bet, idem_key)
    assert third == first


@pytest.mark.asyncio
async def test_teto_slots_animation_trace_never_lands_in_persisted_outcome(session, monkeypatch):
    """ГЛАВНЫЙ структурный инвариант этой фичи: трейс — ПРЕЗЕНТАЦИОННЫЕ данные,
    он уходит в HTTP-ответ и умирает вместе с запросом; аудиторская запись
    (`CasinoGame.outcome`, JSONB) остаётся точно той же, что была.

    Проверяем на РЕАЛЬНО ЗАПИСАННОЙ В POSTGRES строке (сырой SELECT, а не
    объект из identity map сессии — иначе тест сравнивал бы dict сам с собой и
    ничего не доказывал про то, что реально легло в колонку) ТРЕМЯ
    независимыми гардами, каждый из которых закрывает дыру предыдущего:
      1. набор ключей верхнего уровня — в точности прежний;
      2. НИ НА ОДНОМ уровне вложенности нет ни ключа-имени из конверта, ни —
         главное — dict'а ФОРМЫ КАДРА (`op`+`seq`+`blocks`), под каким бы
         именем контейнера он ни лежал;
      3. абсолютный потолок на размер строки в байтах.
    Почему трёх мало по отдельности, и почему именно так — в комментариях у
    каждого гарда ниже. Коротко: гард (1) слеп ко вложенности, (2) слеп к
    размеру, (3) слеп к медианной протечке.

    Конкретный риск, который это стережёт (см. докстринг
    `teto_slot_engine.serialize_animation`): медианный трейс ~17 КБ против ~10.6
    КБ `outcome` — +160% к КАЖДОЙ строке казино данными без аудиторской
    ценности; а патологический спин добавил бы ~21 МБ JSON внутрь той же
    транзакции `_settle`, которая держит row-lock на `chat_bank`."""
    chat_id = -100900049
    user_id = 900049
    await _ensure_user(session, user_id)
    await _fund(session, chat_id, user_id)
    await economy_service.credit_bank(
        session, chat_id, 1_000_000, kind="test_seed", ref_id="test_teto_anim_outcome_seed_bank"
    )
    await session.commit()
    monkeypatch.setattr(casino_service, "_check_slots_throttle", lambda user_id: None)

    idem_key = "test_teto_animation_not_in_outcome"
    bet = _min_valid_teto_bet()

    sink: dict = {}
    result = await casino_service.play_teto_slots(
        session, chat_id, user_id, bet, idem_key, animation_sink=sink
    )

    # Трейс ЕСТЬ в том, что получил вызывающий (иначе тест ниже был бы
    # тривиально зелёным и на сломанной реализации, которая вообще ничего не
    # записывает).
    assert sink["ops"], "фронту нечего анимировать — трейс не собрался"

    persisted = (
        await session.execute(
            text("SELECT outcome FROM casino_games WHERE user_id = :u AND idem_key = :k"),
            {"u": user_id, "k": idem_key},
        )
    ).scalar_one()

    assert set(persisted) == {
        "initial_blocks", "base_round", "scatter_count", "freespins_awarded",
        "freespins_played", "freespins_hard_cap_hit", "fs_round_records",
        "ladder_final_state", "total_payout", "final_blocks",
    }, "форма outcome обязана остаться в точности прежней"
    assert persisted == result["outcome"]

    envelope_keys = {
        "ops", "ops_recorded", "ops_total", "rounds_recorded", "rounds_total",
        "complete_through_round", "truncated", "truncated_reason", "version",
        "payout_paid", "payout_engine_total", "bank_capped",
        "ladder_thresholds", "ladder_max_score",
        "animation", "trace",
    }

    def _assert_no_envelope_key(node, path: str) -> None:
        """Рекурсивный обход всего JSONB: маркерный ключ конверта анимации не
        должен встретиться НИ НА ОДНОМ уровне (например внутри
        `fs_round_records[i]`, если кто-то однажды решит "приложить трейс
        раунда к записи раунда").

        ВСПОМОГАТЕЛЬНАЯ проверка, НЕ основная: она ловит только протечку,
        сохранившую ИМЯ контейнера. Основная — `_assert_no_animation_frame`
        ниже (по форме кадра) плюс байтовый потолок."""
        if isinstance(node, dict):
            leaked = envelope_keys & set(node)
            assert not leaked, f"ключи анимации {sorted(leaked)} утекли в outcome по пути {path}"
            for key, value in node.items():
                _assert_no_envelope_key(value, f"{path}.{key}")
        elif isinstance(node, list):
            for i, value in enumerate(node):
                _assert_no_envelope_key(value, f"{path}[{i}]")

    _assert_no_envelope_key(persisted, "outcome")

    # Опознаём КАДР ПО ФОРМЕ, а не по имени контейнера. Ровно эту дыру
    # оставляла проверка выше: ОДНА строка в `play_teto_slots.compute()` —
    # `outcome["base_round"]["frames"] = serialize_animation(trace)["ops"]` —
    # проходила мимо всего набора, потому что (а) проверка набора ключей
    # смотрит только ВЕРХНИЙ уровень, (б) чёрный список перечисляет имена
    # КОНВЕРТА, а не кадра, и правдоподобное имя контейнера
    # (`frames`/`steps`/`replay`/`_debug`) в нём отсутствует по определению —
    # угадать все имена нельзя в принципе. Форма же кадра фиксирована схемой:
    # `op` + `seq` + `blocks` в одном dict'е — это трейс, как бы ни назывался
    # список, в котором он лежит. Легитимный `outcome` эту тройку не даёт
    # нигде: доски раундов лежат рядом с `raw_round_payout`/`round_index` и
    # ключей `op`/`seq` не имеют вовсе.
    frame_marker_keys = {"op", "seq", "blocks"}

    def _assert_no_animation_frame(node, path: str) -> None:
        if isinstance(node, dict):
            assert not frame_marker_keys <= set(node), (
                f"по пути {path} лежит КАДР анимации (op+seq+blocks) — трейс утёк в "
                f"outcome под именем контейнера, которого нет ни в одном чёрном списке"
            )
            for key, value in node.items():
                _assert_no_animation_frame(value, f"{path}.{key}")
        elif isinstance(node, list):
            for i, value in enumerate(node):
                _assert_no_animation_frame(value, f"{path}[{i}]")

    _assert_no_animation_frame(persisted, "outcome")

    # Размерная сторона того же инварианта: строка в БД осталась "маленькой",
    # хотя трейс того же спина заведомо крупнее — прямое численное
    # доказательство, что раздувание строки не состоялось.
    outcome_bytes = len(json.dumps(persisted).encode())
    animation_bytes = len(json.dumps(sink).encode())

    # Трейс обязан быть содержательным — иначе всё выше зелено тривиально и на
    # реализации, которая просто ничего не собирает (минимум раунда — 4 op'а:
    # fill/evaluate/drill_hunt/round_end, каждый с полной доской 6x6).
    assert sink["ops_total"] >= 4, "трейс подозрительно пуст — проверять было бы нечего"
    assert animation_bytes > outcome_bytes // 4, (
        f"анимация ({animation_bytes} Б) неправдоподобно мала против outcome "
        f"({outcome_bytes} Б) — похоже, кадры не собрались"
    )

    # АБСОЛЮТНЫЙ ПОТОЛОК СТРОКИ — единственный гард, который не обходится
    # переименованием ключа. 131 072 Б (128 КиБ) выбраны не «на глаз», а по
    # замеру `outcome` на 20 000 настоящих спинов (`random.Random(0..19999)`,
    # ставка на линию 10): медиана 10 580 Б, p95 36 210, p99 43 552,
    # p99.9 61 236, МАКСИМУМ 76 382. Потолок = ~1.7x измеренного максимума и
    # ~12x медианы — запас, которого хватает и на естественный рост схемы
    # `outcome`, и на хвост длиннее наблюдённого (максимум за 20 000 спинов —
    # 19 раундов; структурный предел раундов выше, но такие спины требуют
    # почти полностью скаттерной доски).
    # Честно про границы этого гарда — измерено на той же протечке
    # (`outcome["base_round"]["frames"] = serialize_animation(trace)["ops"]`,
    # 3 000 спинов): она растит медиану 10 580 -> 28 341 Б (+168%), p95
    # 36 499 -> 187 498, p99 43 911 -> 232 060, максимум 76 649 -> 427 280.
    # Потолок 128 КиБ ловит 1 046 таких строк из 3 000 (35% — все спины с
    # фриспинами) и НЕ ловит остальные 65%, которые остаются под ним. Тихую
    # медианную протечку ловит проверка ФОРМЫ выше (100% случаев), байтовый
    # потолок берёт на себя катастрофический хвост (теоретически +490 КиБ в
    # одной строке, ~57 ГБ таблицы+TOAST на 10^6 спинов) и любой будущий
    # неограниченный блоб под любым именем. Оба гарда нужны: ни один не
    # покрывает случай другого. Ложных срабатываний на честных строках нет —
    # 0 из 3 000 (и 0 из 20 000 в основном замере).
    # Если этот assert когда-нибудь упадёт на ЧЕСТНОЙ строке — перемерить
    # распределение и двигать потолок осознанно, а не «чуть выше факта».
    max_outcome_bytes = 128 * 1024
    assert outcome_bytes <= max_outcome_bytes, (
        f"строка casino_games.outcome раздулась до {outcome_bytes} Б при потолке "
        f"{max_outcome_bytes} Б — в JSONB поехало что-то, чего там быть не должно "
        f"(INSERT идёт ВНУТРИ транзакции `_settle`, держащей row-lock на chat_bank)"
    )

    print(
        f"\n[teto animation] outcome в БД: {outcome_bytes} Б (потолок {max_outcome_bytes} Б), "
        f"анимация в ответе: {animation_bytes} Б (в БД не попала)"
    )


@pytest.mark.asyncio
async def test_teto_slots_real_spin_full_payload_including_animation_round_trips_through_json(
    session, monkeypatch
):
    """Тот же safety-net, что `test_teto_slots_real_spin_outcome_round_trips_
    through_json` выше, но на ПОЛНОМ пейлоаде ответа роута (`outcome` +
    `animation` в одном dict'е) и на РЕАЛЬНОМ (не форсированном) `_rng`.

    Зачем отдельно от outcome-теста: конверт анимации содержит структуры,
    которых в `outcome` нет вообще — `MegaBlock`-доски на КАЖДОМ кадре (включая
    кадры внутри фриспин-раундов), `frozenset` выигравших id волны Дрель-Ханта
    и `tuple` описаний линий. Забытая конверсия любого из них уронила бы
    `json.dumps` на реальном пользовательском спине уже в FastAPI, ПОСЛЕ того
    как деньги закоммичены — то есть игрок заплатил бы за спин и получил 500.
    Крутим несколько настоящих спинов подряд, пытаясь застать ветку с
    фриспинами (там кадров больше всего и вложенность максимальная)."""
    chat_id = -100900050
    user_id = 900050
    await _ensure_user(session, user_id)
    await _fund(session, chat_id, user_id)
    await economy_service.credit_bank(
        session, chat_id, 1_000_000, kind="test_seed", ref_id="test_teto_anim_json_seed_bank"
    )
    await session.commit()
    monkeypatch.setattr(casino_service, "_check_slots_throttle", lambda user_id: None)

    bet = _min_valid_teto_bet()
    hit_freespins = False
    hit_drill_hunt_wave = False

    for i in range(40):
        sink: dict = {}
        result = await casino_service.play_teto_slots(
            session, chat_id, user_id, bet, f"test_teto_anim_payload_{i}", animation_sink=sink
        )
        payload = {**result, "user_balance_after": 12345, "animation": sink or None}

        round_tripped = json.loads(json.dumps(payload))
        assert round_tripped == payload

        # Кадры трейса обязаны быть теми же plain-dict'ами, что доски в outcome
        # (ОДНА кодировка доски на весь ответ — фронт парсит одну структуру).
        for op in sink["ops"]:
            for block in op["blocks"]:
                assert set(block) == {"block_id", "symbol_id", "row", "col", "height", "width"}
        assert sink["ops"][0]["blocks"] == result["outcome"]["initial_blocks"]

        if result["outcome"]["freespins_played"] > 0:
            hit_freespins = True
        if any(op["op"] == "drill_hunt" and op["wave_fired"] for op in sink["ops"]):
            hit_drill_hunt_wave = True
        if hit_freespins and hit_drill_hunt_wave:
            break

    if not (hit_freespins and hit_drill_hunt_wave):
        print(
            "\n[test_teto_slots_real_spin_full_payload...] за 40 реальных спинов не удалось "
            f"застать всё (фриспины={hit_freespins}, волна Дрель-Ханта={hit_drill_hunt_wave}) "
            "— не блокирующе, round-trip выше уже доказан на реальных спинах"
        )


# --- Блэкджек: двойной натурал (push 1.0x) — редкая ветка settle_outcome ----


@pytest.mark.asyncio
async def test_blackjack_double_natural_pushes_refunds_stake(session, monkeypatch):
    """Аудит-регрессия: если И игрок, И дилер получают натурал (21 на первых
    двух картах) на раздаче start_blackjack, исход должен быть "push" —
    ставка просто возвращается (1.0x), НЕ 2.5x (обычная натурал-выплата) и
    НЕ 0x. Эта ветка (`settle_outcome`'s `if natural: return ("push", 1.0) if
    dealer_natural else ...`) никогда раньше не форсировалась ни одним
    тестом репозитория — все существующие натурал-тесты дают дилеру заведомо
    не-натуральную руку."""
    chat_id = -100900019
    user_id = 900019
    await _ensure_user(session, user_id)
    balance_before = await _fund(session, chat_id, user_id)
    await economy_service.credit_bank(
        session, chat_id, 100_000, kind="test_seed", ref_id="test_bj_double_natural_seed_bank"
    )
    await session.commit()

    # p1=A,p2=K (натурал 21), d1=A,d2=Q (тоже натурал 21)
    monkeypatch.setattr(
        casino_service, "_rng", _FixedDeckRng(["A♠", "K♠", "A♥", "Q♥"])
    )

    bet = 100
    result = await casino_service.start_blackjack(
        session, chat_id, user_id, bet, "test_bj_double_natural"
    )

    assert result["status"] == "settled"
    assert result["outcome"]["result"] == "push"
    # ровно возврат ставки — НЕ int(bet*2.5) и НЕ 0
    assert result["payout"] == bet
    assert await _get_user_balance(session, chat_id, user_id) == balance_before

    game = await _get_casino_game(session, user_id, "test_bj_double_natural")
    assert game.payout == bet
    assert blackjack_engine.is_natural(game.state["dealer"]) is True


# --- Рулетка: зеро (spin=0) — единственная спецкейс-ветка -------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bet_type,bet_value",
    [
        ("color", "red"),
        ("color", "black"),
        ("parity", "even"),
        ("parity", "odd"),
        ("half", "low"),
        ("half", "high"),
        ("dozen", 1),
    ],
)
async def test_roulette_zero_loses_external_bets(session, monkeypatch, bet_type, bet_value):
    """Аудит-регрессия: 0 в европейской рулетке проигрывает ВСЕ "внешние"
    ставки (color/parity/half/dozen), независимо от bet_value. randint=0
    никогда раньше не форсировался ни одним тестом."""
    chat_id = -100900020
    user_id = 900020
    await _ensure_user(session, user_id)
    balance_before = await _fund(session, chat_id, user_id)
    bank_before = await _get_bank_balance(session, chat_id)

    monkeypatch.setattr(casino_service, "_rng", _ForcedRng(randint_value=0))

    bet = 10
    idem_key = f"test_roulette_zero_loses_{bet_type}_{bet_value}"
    result = await casino_service.play_roulette(
        session, chat_id, user_id, bet, bet_type, bet_value, idem_key
    )

    assert result["payout"] == 0
    assert result["outcome"]["won"] is False
    assert result["outcome"]["spin"] == 0
    assert await _get_user_balance(session, chat_id, user_id) == balance_before - bet
    assert await _get_bank_balance(session, chat_id) == bank_before + bet


@pytest.mark.asyncio
async def test_roulette_number_zero_wins_on_zero_spin(session, monkeypatch):
    """Аудит-регрессия: ставка bet_type="number", bet_value=0 ДОЛЖНА
    выигрывать 36x при spin=0 — эта ветка (`bet_type == "number"` в
    `_roulette_win`) стоит РАНЬШЕ спецкейса zero и не обходится им. Раньше
    никогда не форсировалась (bet_value=0 нигде не использовался)."""
    chat_id = -100900021
    user_id = 900021
    await _ensure_user(session, user_id)
    balance_before = await _fund(session, chat_id, user_id)
    await economy_service.credit_bank(
        session, chat_id, 100_000, kind="test_seed", ref_id="test_roulette_number_zero_seed_bank"
    )
    await session.commit()

    monkeypatch.setattr(casino_service, "_rng", _ForcedRng(randint_value=0))

    bet = 10
    result = await casino_service.play_roulette(
        session, chat_id, user_id, bet, "number", 0, "test_roulette_number_zero_win"
    )

    expected_payout = int(bet * casino_service.ROULETTE_NUMBER_MULT)
    assert result["payout"] == expected_payout
    assert result["outcome"]["won"] is True
    assert await _get_user_balance(session, chat_id, user_id) == balance_before - bet + expected_payout


# --- Дайс: направление direction="over" (D-03) -------------------------------


@pytest.mark.asyncio
async def test_dice_over_direction_multiplier_matches_formula(session, monkeypatch):
    """Аудит-регрессия: direction="over" использует ОТДЕЛЬНУЮ формулу
    win_prob=(100-target)/100 (не переиспользует "under"-формулу
    (target-1)/100) — раньше direction="over" не вызывался ни одним тестом
    репозитория. Форсируем roll=100 (единственное значение > target=99)."""
    chat_id = -100900022
    user_id = 900022
    await _ensure_user(session, user_id)
    balance_before = await _fund(session, chat_id, user_id)
    await economy_service.credit_bank(
        session, chat_id, 100_000, kind="test_seed", ref_id="test_dice_over_seed_bank"
    )
    await session.commit()

    target = 99
    direction = "over"
    monkeypatch.setattr(casino_service, "_rng", _ForcedRng(randint_value=100))

    bet = 100
    result = await casino_service.play_dice(
        session, chat_id, user_id, bet, target, direction, "test_dice_over_win"
    )

    # Fraction — независимо от продакшен-кода (см. комментарий в
    # test_dice_multiplier_matches_formula выше).
    exact = bet * (1 - Fraction(2, 100)) / Fraction(100 - target, 100)
    expected_payout = exact.numerator // exact.denominator
    assert result["outcome"]["won"] is True
    assert result["payout"] == expected_payout
    assert await _get_user_balance(session, chat_id, user_id) == balance_before - bet + expected_payout


@pytest.mark.asyncio
async def test_dice_over_direction_loses_when_roll_not_greater(session, monkeypatch):
    """Обратная сторона того же пробела: direction="over" должен ПРОИГРЫВАТЬ,
    когда roll не больше target (roll == target — граница, тоже проигрыш)."""
    chat_id = -100900023
    user_id = 900023
    await _ensure_user(session, user_id)
    balance_before = await _fund(session, chat_id, user_id)

    target = 99
    monkeypatch.setattr(casino_service, "_rng", _ForcedRng(randint_value=target))

    bet = 100
    result = await casino_service.play_dice(
        session, chat_id, user_id, bet, target, "over", "test_dice_over_loss"
    )

    assert result["outcome"]["won"] is False
    assert result["payout"] == 0
    assert await _get_user_balance(session, chat_id, user_id) == balance_before - bet


# --- Rollback ставки при сбое ПОСЛЕ RNG-ролла, ДО финального commit ---------


@pytest.mark.asyncio
async def test_settle_payout_failure_after_rng_does_not_fabricate_or_double_charge(
    session, monkeypatch
):
    """Аудит-регрессия: если ПОСЛЕ RNG-ролла (`compute()` уже вычислил исход,
    ставка уже списана `_debit_stake`) `economy_service.pay_from_bank` падает
    с НЕ-IntegrityError исключением (сбой БД/непредвиденная ошибка), это
    исключение обязано пробрасываться наружу НЕПОДМЕНЁННЫМ — `_settle` не
    должен ни проглотить его и вернуть фальшивый "успешный" исход, ни
    создать строку `CasinoGame` без соответствующей реальной выплаты.

    Прим. про рамки теста: фикстура `session` (tests/conftest.py) кладёт
    сессию поверх УЖЕ открытой внешней транзакции в `join_transaction_mode=
    "rollback_only"` (SQLAlchemy 2.0) — `session.commit()` внутри
    casino_service в этом режиме НЕ делает реальный commit соединения
    (иначе тест не мог бы полагаться на откат всей транзакции в конце), а
    `session.rollback()` откатывает ВСЮ транзакцию теста целиком (включая
    сетап `_ensure_user`/`_fund`), а не только этот раунд — поэтому здесь
    проверяем финансовый инвариант БЕЗ явного rollback: списание ставки уже
    произошло (деньги "в подвешенном" состоянии до итога транзакции), раунд
    НЕ записан как настоящий успех, а наивный повтор с ТЕМ ЖЕ idem_key
    (то, что реально сделал бы клиент после таймаута) обязан явно
    провалиться (`DuplicateRound`), а не списать ставку повторно и не выдать
    молчаливый фальшивый успех."""
    chat_id = -100900024
    user_id = 900024
    await _ensure_user(session, user_id)
    balance_before = await _fund(session, chat_id, user_id)
    bank_before = await _get_bank_balance(session, chat_id)

    monkeypatch.setattr(casino_service, "_rng", _ForcedRng(choice_value="heads"))

    original_pay_from_bank = economy_service.pay_from_bank

    async def _boom(*args, **kwargs):
        raise RuntimeError("симулированный сбой БД после RNG-ролла")

    monkeypatch.setattr(economy_service, "pay_from_bank", _boom)

    bet = 100
    idem_key = "test_settle_payout_failure_blocks_naive_retry"
    with pytest.raises(RuntimeError):
        await casino_service.play_coinflip(session, chat_id, user_id, bet, "heads", idem_key)

    # Ставка уже была списана RNG-роллом/_debit_stake ДО сбоя payout — деньги
    # реально "в подвешенном" состоянии внутри текущей (пока не завершённой)
    # транзакции, ждут исхода самого раунда.
    assert await _get_user_balance(session, chat_id, user_id) == balance_before - bet
    assert await _get_bank_balance(session, chat_id) == bank_before + bet

    # Раунд НЕ записан как успешный/провальный итог — исключение не было
    # тихо проглочено.
    games = (
        await session.execute(
            select(CasinoGame).where(CasinoGame.user_id == user_id, CasinoGame.idem_key == idem_key)
        )
    ).scalars().all()
    assert games == []

    # Наивный повтор с тем же idem_key (клиент после сетевого таймаута) не
    # должен списать ставку повторно и не должен тихо "успешно" завершиться —
    # ref_id `_debit_stake` уже применён, а строки CasinoGame ещё нет, поэтому
    # ожидается явный `DuplicateRound`, а не задвоенное списание.
    monkeypatch.setattr(economy_service, "pay_from_bank", original_pay_from_bank)
    with pytest.raises(casino_service.DuplicateRound):
        await casino_service.play_coinflip(session, chat_id, user_id, bet, "heads", idem_key)

    # Баланс/банк не изменились повторно (никакого двойного списания).
    assert await _get_user_balance(session, chat_id, user_id) == balance_before - bet
    assert await _get_bank_balance(session, chat_id) == bank_before + bet
    games = (
        await session.execute(
            select(CasinoGame).where(CasinoGame.user_id == user_id, CasinoGame.idem_key == idem_key)
        )
    ).scalars().all()
    assert games == []


# --- Crash (стейтфул real-time раунд, D-03: RTP=1-house_edge независимо ------
# --- от точки кэшаута; вся точка-краша/множитель-математика — crash_engine) --
#
# Прошедшее время в crash_cash_out считается СЕРВЕРОМ через
# `casino_service.datetime.utcnow()` — чтобы тесты были детерминированными
# (без реальной задержки/флаки от скорости машины), monkeypatch подменяет
# сам module-level символ `datetime` в casino_service на `_FixedUtcNow` —
# очередь фиксированных значений `.utcnow()` (по одному на вызов, FIFO), та
# же форма, что `_FixedDatetime` в tests/test_daily_twin_service.py.


class _ForcedCrashRng:
    """Тестовый RNG-стаб для crash — `.random()` возвращает фиксированное
    значение вместо реальной случайности `secrets.SystemRandom()`
    (crash_engine.generate_crash_point вызывает только `.random()`)."""

    def __init__(self, value: float):
        self._value = value

    def random(self) -> float:
        return self._value


class _FixedUtcNow:
    """Подмена module-level `datetime` в casino_service — очередь
    фиксированных значений `.utcnow()` (по одному на вызов, FIFO),
    детерминированно контролирует `elapsed` в crash-раунде без реальной
    задержки в тесте. `.fromisoformat` проксируется на настоящий `datetime`
    (crash_cash_out/resolve_crash_timeouts вызывают его на уже
    сериализованном `state["started_at"]`)."""

    def __init__(self, *values: datetime):
        self._values = list(values)

    def utcnow(self) -> datetime:
        return self._values.pop(0)

    def fromisoformat(self, value: str) -> datetime:
        return datetime.fromisoformat(value)


# --- start_crash: эскроу ставки + crash_point никогда не раскрывается -------


@pytest.mark.asyncio
async def test_start_crash_escrows_bet_and_hides_crash_point_while_active(session, monkeypatch):
    chat_id = -100900030
    user_id = 900030
    await _ensure_user(session, user_id)
    balance_before = await _fund(session, chat_id, user_id)

    monkeypatch.setattr(casino_service, "_rng", _ForcedCrashRng(0.5))
    t0 = datetime(2026, 1, 1, 12, 0, 0)
    monkeypatch.setattr(casino_service, "datetime", _FixedUtcNow(t0))

    bet = 100
    result = await casino_service.start_crash(session, chat_id, user_id, bet, "test_crash_start")

    assert result["status"] == "active"
    assert result["bet"] == bet
    assert "crash_point" not in result
    assert "outcome" not in result
    assert "payout" not in result
    assert await _get_user_balance(session, chat_id, user_id) == balance_before - bet

    game = await _get_casino_game(session, user_id, "test_crash_start")
    assert game.game == "crash"
    assert game.status == "active"
    assert isinstance(game.state["crash_point"], str)
    # r=0.5 -> (1-0.02)/(1-0.5) = 1.96 ровно (уже 2 знака, ROUND_DOWN не режет).
    assert Decimal(game.state["crash_point"]) == Decimal("1.96")


@pytest.mark.asyncio
async def test_start_crash_idempotent_on_idem_key(session, monkeypatch):
    chat_id = -100900038
    user_id = 900038
    await _ensure_user(session, user_id)
    balance_before = await _fund(session, chat_id, user_id)

    monkeypatch.setattr(casino_service, "_rng", _ForcedCrashRng(0.5))
    t0 = datetime(2026, 1, 1, 12, 0, 0)
    monkeypatch.setattr(casino_service, "datetime", _FixedUtcNow(t0))

    bet = 100
    idem_key = "test_crash_start_idem"
    first = await casino_service.start_crash(session, chat_id, user_id, bet, idem_key)
    balance_after_first = await _get_user_balance(session, chat_id, user_id)

    second = await casino_service.start_crash(session, chat_id, user_id, bet, idem_key)

    assert second == first
    assert await _get_user_balance(session, chat_id, user_id) == balance_after_first
    assert balance_after_first == balance_before - bet

    games = (
        await session.execute(
            select(CasinoGame).where(CasinoGame.user_id == user_id, CasinoGame.idem_key == idem_key)
        )
    ).scalars().all()
    assert len(games) == 1


# --- crash_cash_out: выигрыш до краша ----------------------------------------


@pytest.mark.asyncio
async def test_crash_cash_out_before_crash_point_pays_floor_bet_times_multiplier(session, monkeypatch):
    chat_id = -100900031
    user_id = 900031
    await _ensure_user(session, user_id)
    balance_before = await _fund(session, chat_id, user_id)
    await economy_service.credit_bank(
        session, chat_id, 100_000, kind="test_seed", ref_id="test_crash_win_seed_bank"
    )
    await session.commit()

    # r=0.9 -> crash_point=(1-0.02)/(1-0.9)=9.80 — заведомо выше любого
    # множителя, который наберётся за несколько секунд forced-elapsed ниже.
    monkeypatch.setattr(casino_service, "_rng", _ForcedCrashRng(0.9))
    t0 = datetime(2026, 1, 1, 12, 0, 0)
    elapsed_seconds = 4
    t1 = t0 + timedelta(seconds=elapsed_seconds)
    monkeypatch.setattr(casino_service, "datetime", _FixedUtcNow(t0, t1))

    bet = 100
    started = await casino_service.start_crash(session, chat_id, user_id, bet, "test_crash_win")
    assert started["status"] == "active"

    result = await casino_service.crash_cash_out(session, chat_id, started["id"], user_id)

    expected_mult = crash_engine.multiplier_at(Decimal(elapsed_seconds))
    expected_payout = int(bet * expected_mult)
    assert result["status"] == "settled"
    assert result["outcome"]["result"] == "cashed_out"
    assert result["outcome"]["multiplier"] == str(expected_mult)
    assert result["cashed_out_multiplier"] == str(expected_mult)
    assert result["payout"] == expected_payout
    assert result["crash_point"] == "9.80"
    assert await _get_user_balance(session, chat_id, user_id) == balance_before - bet + expected_payout

    game = await _get_casino_game(session, user_id, "test_crash_win")
    assert game.status == "settled"
    assert game.payout == expected_payout


# --- crash_cash_out: мгновенный краш на 1.00x --------------------------------


@pytest.mark.asyncio
async def test_crash_cash_out_after_instant_bust_pays_zero(session, monkeypatch):
    chat_id = -100900032
    user_id = 900032
    await _ensure_user(session, user_id)
    balance_before = await _fund(session, chat_id, user_id)
    await economy_service.credit_bank(
        session, chat_id, 100_000, kind="test_seed", ref_id="test_crash_bust_seed_bank"
    )
    await session.commit()

    # r=0.0 < CRASH_HOUSE_EDGE -> мгновенный краш, crash_point == 1.00.
    monkeypatch.setattr(casino_service, "_rng", _ForcedCrashRng(0.0))
    t0 = datetime(2026, 1, 1, 12, 0, 0)
    # elapsed == 0 (та же t0 дважды) — multiplier_at(0)==1.00, а 1.00<1.00
    # ложно, значит раунд уже "лопнул" в самый первый же момент.
    monkeypatch.setattr(casino_service, "datetime", _FixedUtcNow(t0, t0))

    bet = 100
    started = await casino_service.start_crash(session, chat_id, user_id, bet, "test_crash_bust")
    assert started["status"] == "active"

    result = await casino_service.crash_cash_out(session, chat_id, started["id"], user_id)

    assert result["status"] == "settled"
    assert result["outcome"]["result"] == "busted"
    assert result["outcome"]["multiplier"] == "1.00"
    assert result["cashed_out_multiplier"] is None
    assert result["payout"] == 0
    assert result["crash_point"] == "1.00"
    assert await _get_user_balance(session, chat_id, user_id) == balance_before - bet

    game = await _get_casino_game(session, user_id, "test_crash_bust")
    assert game.status == "settled"
    assert game.payout == 0


# --- crash_cash_out: идемпотентный повтор ------------------------------------


@pytest.mark.asyncio
async def test_crash_cash_out_idempotent_second_call_is_noop(session, monkeypatch):
    chat_id = -100900033
    user_id = 900033
    await _ensure_user(session, user_id)
    await _fund(session, chat_id, user_id)
    await economy_service.credit_bank(
        session, chat_id, 100_000, kind="test_seed", ref_id="test_crash_idem_seed_bank"
    )
    await session.commit()

    monkeypatch.setattr(casino_service, "_rng", _ForcedCrashRng(0.9))
    t0 = datetime(2026, 1, 1, 12, 0, 0)
    t1 = t0 + timedelta(seconds=2)
    # Только 2 значения в очереди: start_crash (1) + первый cash_out (1) —
    # второй (replay) cash_out на уже settled раунде НЕ зовёт datetime.utcnow()
    # вовсе (гард идемпотентности коммитит и возвращает раньше), так что
    # очередь не должна опустошиться преждевременно.
    monkeypatch.setattr(casino_service, "datetime", _FixedUtcNow(t0, t1))

    bet = 100
    started = await casino_service.start_crash(session, chat_id, user_id, bet, "test_crash_idem")
    first = await casino_service.crash_cash_out(session, chat_id, started["id"], user_id)
    balance_after_first = await _get_user_balance(session, chat_id, user_id)

    second = await casino_service.crash_cash_out(session, chat_id, started["id"], user_id)

    assert second["payout"] == first["payout"]
    assert second["outcome"] == first["outcome"]
    assert await _get_user_balance(session, chat_id, user_id) == balance_after_first

    games = (
        await session.execute(
            select(CasinoGame).where(CasinoGame.user_id == user_id, CasinoGame.idem_key == "test_crash_idem")
        )
    ).scalars().all()
    assert len(games) == 1


# --- crash_cash_out: IDOR (чужой chat_id) ------------------------------------


@pytest.mark.asyncio
async def test_crash_cash_out_on_foreign_chat_is_rejected_idor(session, monkeypatch):
    """Та же IDOR-защита и то же обоснование, что у `blackjack_action`
    (читай его докстринг): `SELECT ... FOR UPDATE` фильтрует и по `chat_id`,
    не только по `id`/`user_id` — запрос с ЧУЖИМ `chat_id` (раунд начат в
    чате A, запрос выдаёт себя за чат B) обязан не найти раунд вовсе, а не
    молча кэшаутить чужой по контексту раунд."""
    chat_id = -100900034
    foreign_chat_id = -100900035
    user_id = 900034
    await _ensure_user(session, user_id)
    balance_before = await _fund(session, chat_id, user_id)
    await economy_service.credit_bank(
        session, chat_id, 100_000, kind="test_seed", ref_id="test_crash_idor_seed_bank"
    )
    await session.commit()

    monkeypatch.setattr(casino_service, "_rng", _ForcedCrashRng(0.9))
    t0 = datetime(2026, 1, 1, 12, 0, 0)
    monkeypatch.setattr(casino_service, "datetime", _FixedUtcNow(t0))

    bet = 100
    started = await casino_service.start_crash(session, chat_id, user_id, bet, "test_crash_idor")
    assert started["status"] == "active"

    with pytest.raises(casino_service.CasinoError):
        await casino_service.crash_cash_out(session, foreign_chat_id, started["id"], user_id)

    game = (
        await session.execute(select(CasinoGame).where(CasinoGame.id == started["id"]))
    ).scalar_one()
    assert game.status == "active"
    assert await _get_user_balance(session, chat_id, user_id) == balance_before - bet


# --- Bank cap (D-06) ----------------------------------------------------------


@pytest.mark.asyncio
async def test_crash_payout_capped_to_bank_balance(session, monkeypatch):
    chat_id = -100900037
    user_id = 900037
    await _ensure_user(session, user_id)
    await _fund(session, chat_id, user_id)

    small_bank = 5
    await economy_service.credit_bank(
        session, chat_id, small_bank, kind="test_seed", ref_id="test_crash_bank_cap_seed_bank"
    )
    await session.commit()

    monkeypatch.setattr(casino_service, "_rng", _ForcedCrashRng(0.9))  # crash_point 9.80
    t0 = datetime(2026, 1, 1, 12, 0, 0)
    elapsed_seconds = 4
    t1 = t0 + timedelta(seconds=elapsed_seconds)
    monkeypatch.setattr(casino_service, "datetime", _FixedUtcNow(t0, t1))

    bet = 1000
    started = await casino_service.start_crash(session, chat_id, user_id, bet, "test_crash_bank_cap")
    result = await casino_service.crash_cash_out(session, chat_id, started["id"], user_id)

    bank_balance_after = await _get_bank_balance(session, chat_id)
    assert bank_balance_after >= 0

    game = await _get_casino_game(session, user_id, "test_crash_bank_cap")
    assert result["payout"] == game.payout

    expected_uncapped = int(Decimal(bet) * crash_engine.multiplier_at(Decimal(elapsed_seconds)))
    assert result["payout"] < expected_uncapped


# --- Таймаут-резолвер (D-07/D-08-класса: ставка не замораживается навсегда) --


@pytest.mark.asyncio
async def test_resolve_crash_timeouts_force_settles_overdue_round(session, monkeypatch):
    chat_id = -100900036
    user_id = 900036
    await _ensure_user(session, user_id)
    balance_before = await _fund(session, chat_id, user_id)

    # Реальный (не monkeypatched) datetime — resolve_crash_timeouts ниже
    # тоже читает `casino_service.datetime.utcnow()` для "now", а очередь
    # _FixedUtcNow держать синхронизированной с обоими вызовами (start_crash
    # + resolve_crash_timeouts) не нужно, раз started_at сдвигается на 1000с
    # в прошлое (заведомо просрочен при любом разумном "now").
    monkeypatch.setattr(casino_service, "_rng", _ForcedCrashRng(0.5))  # crash_point 1.96

    started = await casino_service.start_crash(session, chat_id, user_id, 100, "test_crash_timeout")
    assert started["status"] == "active"

    # Сдвигаем started_at далеко в прошлое — точка краша 1.96x давно пройдена.
    game = (
        await session.execute(select(CasinoGame).where(CasinoGame.id == started["id"]))
    ).scalar_one()
    state = dict(game.state)
    state["started_at"] = (datetime.utcnow() - timedelta(seconds=1000)).isoformat()
    game.state = state
    await session.commit()

    resolved_count = await casino_service.resolve_crash_timeouts(session)
    assert resolved_count >= 1

    reloaded = (
        await session.execute(select(CasinoGame).where(CasinoGame.id == started["id"]))
    ).scalar_one()
    assert reloaded.status == "settled"
    assert reloaded.payout == 0
    assert reloaded.outcome["result"] == "busted"
    assert reloaded.outcome["multiplier"] == "1.96"
    assert await _get_user_balance(session, chat_id, user_id) == balance_before - 100


@pytest.mark.asyncio
async def test_resolve_crash_timeouts_leaves_fresh_active_round_untouched(session, monkeypatch):
    """Раунд, стартовавший только что (started_at ~ now), НЕ должен
    форс-settle'иться — точка краша ещё не могла быть пройдена по реальному
    времени."""
    chat_id = -100900039
    user_id = 900039
    await _ensure_user(session, user_id)
    await _fund(session, chat_id, user_id)

    monkeypatch.setattr(casino_service, "_rng", _ForcedCrashRng(0.9))  # crash_point 9.80
    started = await casino_service.start_crash(session, chat_id, user_id, 100, "test_crash_no_timeout")
    assert started["status"] == "active"

    resolved_count = await casino_service.resolve_crash_timeouts(session)
    assert resolved_count == 0

    game = (
        await session.execute(select(CasinoGame).where(CasinoGame.id == started["id"]))
    ).scalar_one()
    assert game.status == "active"


# --- peek_crash (read-only polling, баг-репорт 2026-08-07: Михаил) -----------
#
# Косметический тикер на клиенте не знает о реальном crash_point (тот
# намеренно скрыт, пока раунд активен) — без этого опроса игрок узнавал, что
# раунд давно лопнул, только по факту нажатия ЗАБРАТЬ, наблюдался разрыв в
# ~10с между "экран показывает ×8" и "реально лопнуло на ×1.4". `peek_crash`
# должен НИКОГДА не платить/не settle'ить сам — только сообщать `pending_bust`.


@pytest.mark.asyncio
async def test_peek_crash_still_genuinely_active_reveals_nothing(session, monkeypatch):
    chat_id = -100900040
    user_id = 900040
    await _ensure_user(session, user_id)
    await _fund(session, chat_id, user_id)

    # crash_point 9.80 — заведомо выше множителя, набранного за 1с ниже.
    monkeypatch.setattr(casino_service, "_rng", _ForcedCrashRng(0.9))
    t0 = datetime(2026, 1, 1, 12, 0, 0)
    t1 = t0 + timedelta(seconds=1)
    monkeypatch.setattr(casino_service, "datetime", _FixedUtcNow(t0, t1))

    started = await casino_service.start_crash(session, chat_id, user_id, 100, "test_peek_active")

    view = await casino_service.peek_crash(session, chat_id, started["id"], user_id)

    assert view["status"] == "active"
    assert "pending_bust" not in view
    assert "crash_point" not in view
    assert "outcome" not in view

    # Раунд остаётся нетронутым в БД — peek не settle'ит и не платит.
    game = await _get_casino_game(session, user_id, "test_peek_active")
    assert game.status == "active"
    assert game.payout == 0
    assert game.outcome is None


@pytest.mark.asyncio
async def test_peek_crash_reports_pending_bust_without_settling_or_paying(session, monkeypatch):
    chat_id = -100900041
    user_id = 900041
    await _ensure_user(session, user_id)
    balance_before = await _fund(session, chat_id, user_id)
    await economy_service.credit_bank(
        session, chat_id, 100_000, kind="test_seed", ref_id="test_peek_bust_seed_bank"
    )
    await session.commit()

    # r=0.5 -> crash_point 1.96; elapsed 60с гарантированно уже давно за ним.
    monkeypatch.setattr(casino_service, "_rng", _ForcedCrashRng(0.5))
    t0 = datetime(2026, 1, 1, 12, 0, 0)
    t1 = t0 + timedelta(seconds=60)
    monkeypatch.setattr(casino_service, "datetime", _FixedUtcNow(t0, t1))

    started = await casino_service.start_crash(session, chat_id, user_id, 100, "test_peek_bust")

    view = await casino_service.peek_crash(session, chat_id, started["id"], user_id)

    assert view["status"] == "active"  # БД-статус не поменялся — peek не settle'ит
    assert view["pending_bust"] is True
    # Сам crash_point/outcome peek намеренно НЕ раскрывает — единственный
    # путь реального раскрытия/settle это crash_cash_out.
    assert "crash_point" not in view
    assert "outcome" not in view

    game = await _get_casino_game(session, user_id, "test_peek_bust")
    assert game.status == "active"
    assert game.payout == 0
    assert game.outcome is None
    assert await _get_user_balance(session, chat_id, user_id) == balance_before - 100  # только ставка списана


@pytest.mark.asyncio
async def test_peek_crash_on_already_settled_round_returns_full_outcome(session, monkeypatch):
    """Раунд, уже settled кем-то другим (живым кэшаутом здесь, в реальности
    — тоже таймаут-джобой) — peek просто отражает тот же `_crash_view`, что
    и cashout вернул, никакой отдельной логики раскрытия не заводит."""
    chat_id = -100900042
    user_id = 900042
    await _ensure_user(session, user_id)
    await _fund(session, chat_id, user_id)
    await economy_service.credit_bank(
        session, chat_id, 100_000, kind="test_seed", ref_id="test_peek_settled_seed_bank"
    )
    await session.commit()

    monkeypatch.setattr(casino_service, "_rng", _ForcedCrashRng(0.9))
    t0 = datetime(2026, 1, 1, 12, 0, 0)
    t1 = t0 + timedelta(seconds=4)
    monkeypatch.setattr(casino_service, "datetime", _FixedUtcNow(t0, t1))

    started = await casino_service.start_crash(session, chat_id, user_id, 100, "test_peek_settled")
    settled = await casino_service.crash_cash_out(session, chat_id, started["id"], user_id)

    view = await casino_service.peek_crash(session, chat_id, started["id"], user_id)

    assert view["status"] == "settled"
    assert view["outcome"] == settled["outcome"]
    assert view["payout"] == settled["payout"]
    assert view["crash_point"] == settled["crash_point"]
    assert "pending_bust" not in view


@pytest.mark.asyncio
async def test_peek_crash_on_foreign_chat_is_rejected_idor(session, monkeypatch):
    """Тот же 4-way SELECT-фильтр, что `crash_cash_out` — читай его IDOR-тест
    за полным разбором сценария атаки."""
    chat_id = -100900043
    foreign_chat_id = -100900044
    user_id = 900043
    await _ensure_user(session, user_id)
    await _fund(session, chat_id, user_id)
    await _fund(session, foreign_chat_id, user_id)

    monkeypatch.setattr(casino_service, "_rng", _ForcedCrashRng(0.9))
    started = await casino_service.start_crash(session, chat_id, user_id, 100, "test_peek_idor")

    with pytest.raises(casino_service.CasinoError):
        await casino_service.peek_crash(session, foreign_chat_id, started["id"], user_id)


@pytest.mark.asyncio
async def test_peek_crash_on_nonexistent_game_raises(session):
    chat_id = -100900045
    user_id = 900044
    await _ensure_user(session, user_id)

    with pytest.raises(casino_service.CasinoError):
        await casino_service.peek_crash(session, chat_id, 999999999, user_id)
