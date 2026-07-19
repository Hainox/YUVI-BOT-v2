"""Wave 0 тесты STARS-01 (`bot/services/stars_service.py`, `bot/handlers/donate.py`).

Идемпотентность `credit_from_payment` по `telegram_payment_charge_id`
(повтор апдейта при реконнекте polling не задваивает начисление), курс
`STARS_TO_JUVIK_RATE` (D-09), быстрый ack `pre_checkout_query` без единого
обращения к БД/сервисам (Pitfall 2, 10s SLA), парсинг `/donate N` (D-12: строго
> 0). Форма — session-фикстура живого Postgres, тот же паттерн, что
`tests/test_economy_service.py`.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from bot.config import settings
from bot.handlers import donate
from bot.services import stars_service
from common.models.user import User
from common.models.user_balance import UserBalance


async def _ensure_user(session, user_id: int, first_name: str = "Донор") -> None:
    session.add(User(id=user_id, first_name=first_name))
    await session.flush()


async def _get_user_balance(session, chat_id: int, user_id: int) -> int:
    result = await session.execute(
        select(UserBalance.balance).where(
            UserBalance.chat_id == chat_id, UserBalance.user_id == user_id
        )
    )
    return result.scalar_one()


# --- credit_from_payment: идемпотентность по charge_id (STARS-01) -----------


@pytest.mark.asyncio
async def test_duplicate_charge_id_idempotent(session):
    chat_id = -900601
    user_id = 900601
    await _ensure_user(session, user_id)

    charge_id = "test_charge_duplicate"
    stars = 5
    juviks = stars * settings.stars_to_juvik_rate

    credited_first = await stars_service.credit_from_payment(
        session, chat_id, user_id, stars, charge_id
    )
    await session.commit()
    assert credited_first is True
    assert await _get_user_balance(session, chat_id, user_id) == settings.economy_start_bonus + juviks

    credited_second = await stars_service.credit_from_payment(
        session, chat_id, user_id, stars, charge_id
    )
    await session.commit()
    assert credited_second is False
    assert await _get_user_balance(session, chat_id, user_id) == settings.economy_start_bonus + juviks


@pytest.mark.asyncio
async def test_credit_amount_uses_rate(session):
    chat_id = -900602
    user_id = 900602
    await _ensure_user(session, user_id)

    stars = 5
    credited = await stars_service.credit_from_payment(
        session, chat_id, user_id, stars, "test_charge_rate"
    )
    await session.commit()

    assert credited is True
    assert await _get_user_balance(session, chat_id, user_id) == settings.economy_start_bonus + 50


# --- pre_checkout: fast-ack без БД (Pitfall 2, 10s SLA) ----------------------


@pytest.mark.asyncio
async def test_pre_checkout_acks_fast():
    """on_pre_checkout принимает ТОЛЬКО pre_checkout_query (без session/economy_service
    в сигнатуре) — структурная гарантия, что до ack нет ни одного обращения к БД."""
    pre_checkout_query = AsyncMock()

    await donate.on_pre_checkout(pre_checkout_query)

    pre_checkout_query.answer.assert_awaited_once_with(ok=True)


# --- _parse_positive_int (D-12: минимум 1, без верхнего предела) ------------


def test_parse_positive_int():
    assert donate._parse_positive_int("5") == 5
    assert donate._parse_positive_int("0") is None
    assert donate._parse_positive_int("-3") is None
    assert donate._parse_positive_int("abc") is None
    assert donate._parse_positive_int(None) is None
