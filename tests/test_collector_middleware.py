"""Интеграционный тест CollectorMiddleware против живого Postgres (фикстура `session`
из tests/conftest.py — транзакция-на-тест с rollback).

Доказывает Walking Skeleton (DATA-01): реальное текстовое сообщение записывается
в messages + инкрементирует daily_stats, а команды/service-сообщения без
from_user НЕ теряются — handler всегда вызывается.
"""

from __future__ import annotations

from datetime import datetime
from datetime import timezone
from unittest.mock import AsyncMock

import pytest
from aiogram.types import Chat
from aiogram.types import Message
from aiogram.types import User
from sqlalchemy import select

from bot.middleware.collector import CollectorMiddleware
from common.models.daily_stat import DailyStat
from common.models.message import Message as MessageModel


def _make_message(
    message_id: int,
    chat_id: int,
    from_user: User | None,
    text: str | None,
) -> Message:
    chat = Chat(id=chat_id, type="group")
    return Message(
        message_id=message_id,
        date=datetime.now(timezone.utc),
        chat=chat,
        from_user=from_user,
        text=text,
    )


@pytest.mark.asyncio
async def test_collector_middleware_saves_text_message_and_bumps_daily_stat(session):
    chat_id = -100123456789
    user_id = 555000111
    user = User(id=user_id, is_bot=False, first_name="Тест")
    message = _make_message(2001, chat_id, user, "Привет, мир!")

    handler = AsyncMock(return_value="handled")
    middleware = CollectorMiddleware()

    result = await middleware(handler, message, {"session": session})

    assert result == "handled"
    handler.assert_awaited_once_with(message, {"session": session})

    saved = (
        await session.execute(
            select(MessageModel).where(
                MessageModel.chat_id == chat_id,
                MessageModel.telegram_message_id == 2001,
            )
        )
    ).scalar_one()
    assert saved.text == "Привет, мир!"
    assert saved.user_id == user_id
    assert saved.content_type == "text"

    stat = (
        await session.execute(
            select(DailyStat).where(
                DailyStat.chat_id == chat_id,
                DailyStat.user_id == user_id,
            )
        )
    ).scalar_one()
    assert stat.message_count == 1


@pytest.mark.asyncio
async def test_collector_middleware_bumps_daily_stat_on_second_message(session):
    chat_id = -100123456790
    user_id = 555000112
    user = User(id=user_id, is_bot=False, first_name="Тест2")

    handler = AsyncMock(return_value="handled")
    middleware = CollectorMiddleware()

    await middleware(handler, _make_message(3001, chat_id, user, "первое"), {"session": session})
    await middleware(handler, _make_message(3002, chat_id, user, "второе"), {"session": session})

    stat = (
        await session.execute(
            select(DailyStat).where(
                DailyStat.chat_id == chat_id,
                DailyStat.user_id == user_id,
            )
        )
    ).scalar_one()
    assert stat.message_count == 2


@pytest.mark.asyncio
async def test_collector_middleware_is_idempotent_on_retry_of_same_message(session):
    """T-02-04: повторная обработка того же telegram_message_id (ретрай после
    краша до commit) не должна задваивать daily_stats.message_count — сообщение
    пропускается через on_conflict_do_nothing, счётчик не растёт повторно."""
    chat_id = -100123456792
    user_id = 555000113
    user = User(id=user_id, is_bot=False, first_name="Тест3")

    handler = AsyncMock(return_value="handled")
    middleware = CollectorMiddleware()

    same_message = _make_message(5001, chat_id, user, "одно и то же сообщение")
    await middleware(handler, same_message, {"session": session})
    await middleware(handler, same_message, {"session": session})

    stat = (
        await session.execute(
            select(DailyStat).where(
                DailyStat.chat_id == chat_id,
                DailyStat.user_id == user_id,
            )
        )
    ).scalar_one()
    assert stat.message_count == 1


@pytest.mark.asyncio
async def test_collector_middleware_always_calls_handler_when_from_user_is_none(session):
    """Анонимный админ / linked-channel пост (Pitfall 5) — запись пропускается,
    но handler ВСЕГДА вызывается (команды/прочие апдейты не теряются, DATA-01)."""
    chat_id = -100123456791
    message = _make_message(4001, chat_id, from_user=None, text="/start")

    handler = AsyncMock(return_value="handled")
    middleware = CollectorMiddleware()

    result = await middleware(handler, message, {"session": session})

    assert result == "handled"
    handler.assert_awaited_once_with(message, {"session": session})

    saved = (
        await session.execute(
            select(MessageModel).where(
                MessageModel.chat_id == chat_id,
                MessageModel.telegram_message_id == 4001,
            )
        )
    ).scalar_one_or_none()
    assert saved is None
