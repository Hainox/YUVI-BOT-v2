"""Тесты слота "Azumanga" (04.1-02): server-authoritative порт
webapp/slot-data.jsx на Python. Доказывают:

- PAYTABLE == D-06 (пересчитанные числа, RTP ~92.78%), НЕ исходные числа
  черновика webapp/slot-data.jsx (там были другие, "сломанные" по RTP ~39%
  цифры) — источник истины: .planning/phases/04-mini-app-games-mini-app/04-CONTEXT.md
- WEIGHTS == D-05 (веса символов НЕ менялись при пересчёте паутейбла).
- wild (muscle) подставляется вместо любого символа кроме scatter.
- scatter (keffiyeh) платит ТОЛЬКО фриспинами, не по линиям.
- spin_grid возвращает корректную форму сетки 3x5.
- Сэмплированный RTP укладывается в широкий диапазон вокруг 92.78%.
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


def test_scatter_pays_freespins_not_lines():
    # >=3 keffiyeh anywhere on the grid -> freespins, 0 line payout for keffiyeh
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

    game = await _get_casino_game(session, user_id, idem_key)
    assert game.game == "slots"
    assert "grid" in game.outcome
    assert "wins" in game.outcome

    second = await casino_service.play_slots(session, chat_id, user_id, bet_total, idem_key)
    assert second["payout"] == first["payout"]
    assert second["outcome"] == first["outcome"]
    assert await _get_user_balance(session, chat_id, user_id) == balance_after_first

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
