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
from common.models.user import User
from common.models.word_frequency import WordFrequency

MSK = ZoneInfo("Europe/Moscow")


def _today_msk() -> date:
    return datetime.now(MSK).date()


def _since_date(days: int | None) -> date | None:
    """D-06: days=None -> всё время (None). Иначе — дата начала периода
    (включая сегодня, по дате в Europe/Moscow)."""
    if days is None:
        return None
    return _today_msk() - timedelta(days=days)


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
    since_date = _since_date(days)
    if since_date is not None:
        stmt = stmt.where(DailyStat.stat_date >= since_date)

    result = await session.execute(stmt)
    return int(result.scalar_one())


async def get_user_stats(
    session: AsyncSession,
    chat_id: int,
    user_id: int,
    days: int | None = None,
) -> dict:
    """Личная статистика пользователя из daily_stats (для /mystats).

    D-06: days=None — за всё время; иначе — за последние N дней.
    Возвращает: total_messages, active_days (число строк daily_stats —
    каждая строка = один день с активностью), first_active_date,
    last_active_date (в пределах периода, если он задан).
    """
    stmt = select(
        func.coalesce(func.sum(DailyStat.message_count), 0),
        func.count(DailyStat.stat_date),
        func.min(DailyStat.stat_date),
        func.max(DailyStat.stat_date),
    ).where(DailyStat.chat_id == chat_id, DailyStat.user_id == user_id)
    since_date = _since_date(days)
    if since_date is not None:
        stmt = stmt.where(DailyStat.stat_date >= since_date)

    result = await session.execute(stmt)
    total, active_days, first_date, last_date = result.one()
    return {
        "total_messages": int(total),
        "active_days": int(active_days),
        "first_active_date": first_date,
        "last_active_date": last_date,
    }


async def get_top_participants(
    session: AsyncSession,
    chat_id: int,
    days: int | None = None,
    limit: int = 10,
) -> list[dict]:
    """Топ участников чата по сумме message_count из daily_stats (для /who, /top).

    D-06: days=None — за всё время; иначе — за последние N дней.
    Имя резолвится джойном на users — вызывающий код не должен хранить
    сырой telegram id как отображаемое имя.
    """
    total_col = func.sum(DailyStat.message_count).label("total")
    stmt = (
        select(DailyStat.user_id, User.first_name, User.username, total_col)
        .join(User, User.id == DailyStat.user_id)
        .where(DailyStat.chat_id == chat_id)
        .group_by(DailyStat.user_id, User.first_name, User.username)
        .order_by(total_col.desc())
        .limit(limit)
    )
    since_date = _since_date(days)
    if since_date is not None:
        stmt = stmt.where(DailyStat.stat_date >= since_date)

    result = await session.execute(stmt)
    return [
        {
            "user_id": row.user_id,
            "first_name": row.first_name,
            "username": row.username,
            "message_count": int(row.total),
        }
        for row in result.all()
    ]


async def get_streak(session: AsyncSession, chat_id: int, user_id: int) -> int:
    """Длина текущей серии последовательных дней активности (для /streak).

    Считает от самого позднего дня активности пользователя в daily_stats
    назад, пока даты идут без пропусков (stat_date - 1 день). Не привязано
    к "сегодня" — серия остаётся видна, даже если бот проверяется до того,
    как пользователь написал сегодня.
    """
    stmt = (
        select(DailyStat.stat_date)
        .where(
            DailyStat.chat_id == chat_id,
            DailyStat.user_id == user_id,
            DailyStat.message_count > 0,
        )
        .order_by(DailyStat.stat_date.desc())
    )
    result = await session.execute(stmt)
    dates = [row[0] for row in result.all()]
    if not dates:
        return 0

    streak = 1
    for previous, current in zip(dates, dates[1:]):
        if previous - current == timedelta(days=1):
            streak += 1
        else:
            break
    return streak


async def get_peak_day(
    session: AsyncSession,
    chat_id: int,
    days: int | None = None,
) -> tuple[date, int] | None:
    """День с максимальной суммарной активностью чата (для /peakday, /activity).

    D-06: days=None — за всё время; иначе — за последние N дней.
    Возвращает None, если по чату нет данных за период.
    """
    total_col = func.sum(DailyStat.message_count).label("total")
    stmt = (
        select(DailyStat.stat_date, total_col)
        .where(DailyStat.chat_id == chat_id)
        .group_by(DailyStat.stat_date)
        .order_by(total_col.desc())
        .limit(1)
    )
    since_date = _since_date(days)
    if since_date is not None:
        stmt = stmt.where(DailyStat.stat_date >= since_date)

    result = await session.execute(stmt)
    row = result.first()
    if row is None:
        return None
    return row.stat_date, int(row.total)


async def get_top_words(
    session: AsyncSession,
    chat_id: int,
    days: int | None = None,
    limit: int = 10,
) -> list[dict]:
    """Топ слов чата по частоте из word_frequency (для /words, топ в /chatstats/who).

    Примечание: word_frequency не разбит по дням (агрегат без stat_date),
    поэтому days здесь намеренно игнорируется — документированное исключение
    из D-06 (per-day частоты слов вне MVP этой фазы).
    """
    total_col = func.sum(WordFrequency.count).label("total")
    stmt = (
        select(WordFrequency.word, total_col)
        .where(WordFrequency.chat_id == chat_id)
        .group_by(WordFrequency.word)
        .order_by(total_col.desc())
        .limit(limit)
    )
    result = await session.execute(stmt)
    return [{"word": row.word, "count": int(row.total)} for row in result.all()]
