"""Read-only агрегатные запросы статистики.

Читает daily_stats (несёт message_count), НЕ делает COUNT(*) по messages
(та таблица растёт неограниченно после backfill — RESEARCH.md Anti-Patterns).
"""

from __future__ import annotations

from datetime import date
from datetime import datetime
from datetime import timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from common.models.daily_stat import DailyStat

MSK = ZoneInfo("Europe/Moscow")


def _today_msk() -> date:
    return datetime.now(MSK).date()


async def get_chat_message_count(
    session: AsyncSession,
    chat_id: int,
    days: int | None = None,
) -> int:
    """Сумма message_count по чату из daily_stats.

    D-06: days=None (по умолчанию) — за всё время; иначе — за последние N дней
    (включая сегодня, по дате в Europe/Moscow).
    """
    stmt = select(func.coalesce(func.sum(DailyStat.message_count), 0)).where(
        DailyStat.chat_id == chat_id,
    )
    if days is not None:
        since_date = _today_msk() - timedelta(days=days)
        stmt = stmt.where(DailyStat.stat_date >= since_date)

    result = await session.execute(stmt)
    return int(result.scalar_one())
