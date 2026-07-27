"""Тесты прогрессивного джекпота слота (CASINO-06, bot/services/jackpot_service.py)
против живого Postgres (фикстура `session` — транзакция-на-тест). Доказывают:

- Пул чата создаётся лениво и засеивается `settings.slot_jackpot_seed` при
  первом обращении (`_get_or_seed_pool_locked`/`get_pool`).
- `contribute_and_maybe_award` на каждый вызов пополняет пул на
  `int(bet * settings.slot_jackpot_skim_pct)` независимо от исхода броска.
- На неудаче (rng.randint != 1) пул просто растёт, деньги игроку не платятся.
- На удаче (rng.randint == 1) выплачивается ВЕСЬ накопленный пул через
  `economy_service.pay_from_bank` (капается остатком банка, как обычный
  выигрыш казино, D-06), пул сбрасывается на seed.
- `casino_service.play_slots` зовёт джекпот ТОЛЬКО для подтверждённо нового
  спина — replay того же idem_key возвращает `jackpot=None`, пул не растёт и
  кубик не бросается повторно.

RNG форсируется через простой `_ForcedRng`-стаб (`.randint` фиксированное
значение) — никогда не проверяем реальную случайность.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from bot.config import settings
from bot.services import casino_service
from bot.services import economy_service
from bot.services import jackpot_service
from common.models.chat_bank import ChatBank
from common.models.slot_jackpot import SlotJackpot
from common.models.user import User


async def _ensure_user(session, user_id: int, first_name: str = "Тест") -> None:
    session.add(User(id=user_id, first_name=first_name))
    await session.flush()


async def _fund(session, chat_id: int, user_id: int) -> None:
    await economy_service.get_balance(session, chat_id, user_id)


async def _get_bank_balance(session, chat_id: int) -> int:
    result = await session.execute(select(ChatBank.balance).where(ChatBank.chat_id == chat_id))
    return result.scalar_one_or_none() or 0


async def _get_pool_row(session, chat_id: int) -> SlotJackpot | None:
    return (
        await session.execute(select(SlotJackpot).where(SlotJackpot.chat_id == chat_id))
    ).scalar_one_or_none()


class _ForcedRng:
    """Форсирует конкретный исход `randint` — 1 (выигрыш) либо любое другое
    значение (проигрыш)."""

    def __init__(self, randint_value: int):
        self._randint_value = randint_value

    def randint(self, a: int, b: int) -> int:
        return self._randint_value


# --- Ленивое создание/засев пула ---------------------------------------------


@pytest.mark.asyncio
async def test_get_pool_seeds_on_first_read(session):
    chat_id = -100900201
    assert await jackpot_service.get_pool(session, chat_id) == settings.slot_jackpot_seed
    # get_pool сам по себе НЕ создаёт строку (без блокировки, только чтение).
    assert await _get_pool_row(session, chat_id) is None


@pytest.mark.asyncio
async def test_contribute_seeds_pool_row_on_first_spin(session):
    chat_id = -100900202
    user_id = 900202
    await _ensure_user(session, user_id)
    await _fund(session, chat_id, user_id)
    await session.commit()

    bet = 200
    jackpot = await jackpot_service.contribute_and_maybe_award(
        session, chat_id, user_id, bet, "test_jackpot_seed", _ForcedRng(randint_value=2)
    )
    await session.commit()

    expected_pool = settings.slot_jackpot_seed + int(bet * settings.slot_jackpot_skim_pct)
    assert jackpot == {"won": False, "pool": expected_pool}
    row = await _get_pool_row(session, chat_id)
    assert row is not None
    assert row.pool == expected_pool


# --- Пополнение без выигрыша ---------------------------------------------------


@pytest.mark.asyncio
async def test_contribute_accumulates_across_spins(session):
    chat_id = -100900203
    user_id = 900203
    await _ensure_user(session, user_id)
    await _fund(session, chat_id, user_id)
    await session.commit()

    bet = 500
    skim = int(bet * settings.slot_jackpot_skim_pct)
    rng = _ForcedRng(randint_value=42)  # никогда не 1 -> никогда не выигрывает

    first = await jackpot_service.contribute_and_maybe_award(
        session, chat_id, user_id, bet, "test_jackpot_accum_1", rng
    )
    await session.commit()
    second = await jackpot_service.contribute_and_maybe_award(
        session, chat_id, user_id, bet, "test_jackpot_accum_2", rng
    )
    await session.commit()

    assert first == {"won": False, "pool": settings.slot_jackpot_seed + skim}
    assert second == {"won": False, "pool": settings.slot_jackpot_seed + 2 * skim}


# --- Выигрыш и сброс пула -------------------------------------------------------


@pytest.mark.asyncio
async def test_contribute_award_pays_full_pool_and_resets(session):
    chat_id = -100900204
    user_id = 900204
    await _ensure_user(session, user_id)
    await _fund(session, chat_id, user_id)
    await economy_service.credit_bank(
        session, chat_id, 1_000_000, kind="test_seed", ref_id="test_jackpot_award_bank_seed"
    )
    await session.commit()

    bet = 300
    skim = int(bet * settings.slot_jackpot_skim_pct)
    expected_pool_before_award = settings.slot_jackpot_seed + skim

    jackpot = await jackpot_service.contribute_and_maybe_award(
        session, chat_id, user_id, bet, "test_jackpot_win", _ForcedRng(randint_value=1)
    )
    await session.commit()

    assert jackpot == {
        "won": True,
        "amount": expected_pool_before_award,
        "pool": settings.slot_jackpot_seed,
    }
    row = await _get_pool_row(session, chat_id)
    assert row.pool == settings.slot_jackpot_seed


@pytest.mark.asyncio
async def test_contribute_award_capped_to_bank_balance(session):
    chat_id = -100900205
    user_id = 900205
    await _ensure_user(session, user_id)
    await _fund(session, chat_id, user_id)

    small_bank = 7
    await economy_service.credit_bank(
        session, chat_id, small_bank, kind="test_seed", ref_id="test_jackpot_cap_bank_seed"
    )
    await session.commit()

    bet = 500
    jackpot = await jackpot_service.contribute_and_maybe_award(
        session, chat_id, user_id, bet, "test_jackpot_win_capped", _ForcedRng(randint_value=1)
    )
    await session.commit()

    # Пул (seed + skim) заведомо больше крошечного банка -> выплата урезана
    # до остатка банка (D-06, тот же примитив pay_from_bank, что обычный
    # выигрыш казино), но пул всё равно сбрасывается на seed.
    assert jackpot["won"] is True
    assert jackpot["amount"] <= small_bank
    assert jackpot["pool"] == settings.slot_jackpot_seed
    assert await _get_bank_balance(session, chat_id) >= 0


# --- Wiring в casino_service.play_slots: только для НОВОГО спина -------------


@pytest.mark.asyncio
async def test_play_slots_replay_does_not_touch_jackpot(session, monkeypatch):
    chat_id = -100900206
    user_id = 900206
    await _ensure_user(session, user_id)
    await _fund(session, chat_id, user_id)
    await session.commit()

    from tests.test_slot_engine import _ForcedGridRng
    from bot.services import slot_engine

    forced_symbols = ["sakaki", "bath-chibi", "osaka-stand", "dog", "gasp"] * 3
    monkeypatch.setattr(casino_service, "_rng", _ForcedGridRng(forced_symbols))

    idem_key = "test_jackpot_replay_guard"
    bet_total = 10 * slot_engine.TOTAL_LINES

    first = await casino_service.play_slots(session, chat_id, user_id, bet_total, idem_key)
    assert first["jackpot"] is not None
    pool_after_first = first["jackpot"]["pool"]

    second = await casino_service.play_slots(session, chat_id, user_id, bet_total, idem_key)
    assert second["jackpot"] is None

    row = await _get_pool_row(session, chat_id)
    assert row.pool == pool_after_first
