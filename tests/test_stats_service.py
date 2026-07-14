"""Интеграционный тест stats_service.get_chat_message_count против живого Postgres.

Доказывает D-06: all-time сумма по умолчанию и корректный учёт периода `days`.
"""

from __future__ import annotations

from datetime import date
from datetime import datetime
from datetime import timedelta
from zoneinfo import ZoneInfo

import pytest

from bot.services import stats_service
from common.models.daily_stat import DailyStat
from common.models.user import User

MSK = ZoneInfo("Europe/Moscow")


async def _seed_daily_stats(session, chat_id: int, user_id: int, rows: list[tuple[date, int]]) -> None:
    session.add(User(id=user_id, first_name="Тест"))
    await session.flush()
    for stat_date, count in rows:
        session.add(
            DailyStat(chat_id=chat_id, user_id=user_id, stat_date=stat_date, message_count=count)
        )
    await session.flush()


@pytest.mark.asyncio
async def test_get_chat_message_count_all_time_sums_everything(session):
    chat_id = -100987654321
    user_id = 700000001
    today = datetime.now(MSK).date()

    await _seed_daily_stats(
        session,
        chat_id,
        user_id,
        [
            (today, 5),
            (today - timedelta(days=10), 3),
            (today - timedelta(days=100), 7),
        ],
    )

    total = await stats_service.get_chat_message_count(session, chat_id, days=None)

    assert total == 15


@pytest.mark.asyncio
async def test_get_chat_message_count_respects_period_argument(session):
    chat_id = -100987654322
    user_id = 700000002
    today = datetime.now(MSK).date()

    await _seed_daily_stats(
        session,
        chat_id,
        user_id,
        [
            (today, 4),
            (today - timedelta(days=5), 6),
            (today - timedelta(days=40), 20),
        ],
    )

    total_last_30 = await stats_service.get_chat_message_count(session, chat_id, days=30)

    assert total_last_30 == 10


@pytest.mark.asyncio
async def test_get_chat_message_count_returns_zero_for_unknown_chat(session):
    total = await stats_service.get_chat_message_count(session, chat_id=-1, days=None)

    assert total == 0
