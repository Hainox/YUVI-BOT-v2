"""Сервис записи сообщений: upsert пользователя, идемпотентная запись сообщения,
инкремент дневной агрегированной статистики (daily_stats).

Вызывается из CollectorMiddleware. На этом срезе (Walking Skeleton) пишет только
текстовую часть: text, reply_to_telegram_message_id, message_thread_id,
content_type. Медиа/частотные словари — план 04.
"""

from __future__ import annotations

from zoneinfo import ZoneInfo

from aiogram.types import Message
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from common.models.daily_stat import DailyStat
from common.models.message import Message as MessageModel
from common.models.user import User

MSK = ZoneInfo("Europe/Moscow")


async def save_message(session: AsyncSession, event: Message) -> None:
    """Пишет пользователя + сообщение (идемпотентно) + инкремент daily_stats.

    Требует, чтобы event.from_user не был None (проверяется в CollectorMiddleware
    до вызова — анонимные админы/боты сюда не попадают).
    """
    user = event.from_user
    assert user is not None

    user_stmt = pg_insert(User).values(
        id=user.id,
        username=user.username,
        first_name=user.first_name or "",
    )
    user_stmt = user_stmt.on_conflict_do_update(
        index_elements=["id"],
        set_={
            "username": user_stmt.excluded.username,
            "first_name": user_stmt.excluded.first_name,
        },
    )
    await session.execute(user_stmt)

    message_stmt = pg_insert(MessageModel).values(
        telegram_message_id=event.message_id,
        chat_id=event.chat.id,
        user_id=user.id,
        text=event.text,
        reply_to_telegram_message_id=(
            event.reply_to_message.message_id if event.reply_to_message else None
        ),
        message_thread_id=event.message_thread_id,
        content_type=event.content_type.value,
    )
    message_stmt = message_stmt.on_conflict_do_nothing(
        index_elements=["chat_id", "telegram_message_id"],
    )
    await session.execute(message_stmt)

    stat_date = event.date.astimezone(MSK).date()
    daily_stat_stmt = pg_insert(DailyStat).values(
        chat_id=event.chat.id,
        user_id=user.id,
        stat_date=stat_date,
        message_count=1,
    )
    daily_stat_stmt = daily_stat_stmt.on_conflict_do_update(
        index_elements=["chat_id", "user_id", "stat_date"],
        set_={"message_count": DailyStat.message_count + daily_stat_stmt.excluded.message_count},
    )
    await session.execute(daily_stat_stmt)
