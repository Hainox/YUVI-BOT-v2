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
from aiogram.types import Document
from aiogram.types import Message
from aiogram.types import MessageOriginUser
from aiogram.types import Sticker
from aiogram.types import User
from aiogram.types import Video
from aiogram.types import Voice
from sqlalchemy import select

from bot.middleware.collector import CollectorMiddleware
from bot.services import message_service
from common.models.daily_stat import DailyStat
from common.models.emoji_frequency import EmojiFrequency
from common.models.message import Message as MessageModel
from common.models.message_edit import MessageEdit
from common.models.word_frequency import WordFrequency


def _make_message(
    message_id: int,
    chat_id: int,
    from_user: User | None,
    text: str | None,
    **extra,
) -> Message:
    chat = Chat(id=chat_id, type="group")
    return Message(
        message_id=message_id,
        date=datetime.now(timezone.utc),
        chat=chat,
        from_user=from_user,
        text=text,
        **extra,
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


# --- Task 3: медиа всех типов (Pitfall 6) --------------------------------


@pytest.mark.asyncio
async def test_media_content_types(session):
    """DATA-02: медиа всех типов (video/voice/document/sticker), не только
    photo/sticker, сохраняются с file_id/file_unique_id/content_type."""
    chat_id = -100123456800
    user_id = 555000200
    user = User(id=user_id, is_bot=False, first_name="Медиа")

    handler = AsyncMock(return_value="handled")
    middleware = CollectorMiddleware()

    cases = [
        (
            6001,
            {"video": Video(file_id="vid1", file_unique_id="vidu1", width=100, height=100, duration=5)},
            "video",
            "vid1",
            "vidu1",
        ),
        (
            6002,
            {"voice": Voice(file_id="voi1", file_unique_id="voiu1", duration=3)},
            "voice",
            "voi1",
            "voiu1",
        ),
        (
            6003,
            {"document": Document(file_id="doc1", file_unique_id="docu1")},
            "document",
            "doc1",
            "docu1",
        ),
        (
            6004,
            {
                "sticker": Sticker(
                    file_id="stk1",
                    file_unique_id="stku1",
                    type="regular",
                    width=512,
                    height=512,
                    is_animated=False,
                    is_video=False,
                )
            },
            "sticker",
            "stk1",
            "stku1",
        ),
    ]

    for message_id, extra, expected_content_type, expected_file_id, expected_file_unique_id in cases:
        message = _make_message(message_id, chat_id, user, text=None, **extra)
        await middleware(handler, message, {"session": session})

        saved = (
            await session.execute(
                select(MessageModel).where(
                    MessageModel.chat_id == chat_id,
                    MessageModel.telegram_message_id == message_id,
                )
            )
        ).scalar_one()
        assert saved.content_type == expected_content_type
        assert saved.media_file_id == expected_file_id
        assert saved.media_file_unique_id == expected_file_unique_id


@pytest.mark.asyncio
async def test_forwarded_message_is_marked_is_forwarded(session):
    """DATA-02: is_forwarded=True при наличии forward_origin (не устаревший forward_from)."""
    chat_id = -100123456801
    user_id = 555000201
    user = User(id=user_id, is_bot=False, first_name="Форвард")
    origin_user = User(id=999000111, is_bot=False, first_name="Автор")

    handler = AsyncMock(return_value="handled")
    middleware = CollectorMiddleware()

    message = _make_message(
        7001,
        chat_id,
        user,
        text="переслано",
        forward_origin=MessageOriginUser(
            type="user", date=datetime.now(timezone.utc), sender_user=origin_user
        ),
    )
    await middleware(handler, message, {"session": session})

    saved = (
        await session.execute(
            select(MessageModel).where(
                MessageModel.chat_id == chat_id,
                MessageModel.telegram_message_id == 7001,
            )
        )
    ).scalar_one()
    assert saved.is_forwarded is True


@pytest.mark.asyncio
async def test_non_forwarded_message_is_not_marked_is_forwarded(session):
    chat_id = -100123456802
    user_id = 555000202
    user = User(id=user_id, is_bot=False, first_name="НеФорвард")

    handler = AsyncMock(return_value="handled")
    middleware = CollectorMiddleware()

    message = _make_message(7002, chat_id, user, text="обычное сообщение")
    await middleware(handler, message, {"session": session})

    saved = (
        await session.execute(
            select(MessageModel).where(
                MessageModel.chat_id == chat_id,
                MessageModel.telegram_message_id == 7002,
            )
        )
    ).scalar_one()
    assert saved.is_forwarded is False


@pytest.mark.asyncio
async def test_collector_middleware_bumps_word_and_emoji_frequency(session):
    """DATA-03: CollectorMiddleware бампает word_frequency/emoji_frequency
    в той же транзакции, что и save_message."""
    chat_id = -100123456803
    user_id = 555000203
    user = User(id=user_id, is_bot=False, first_name="Частоты")

    handler = AsyncMock(return_value="handled")
    middleware = CollectorMiddleware()

    message = _make_message(8001, chat_id, user, text="привет привет 🔥")
    await middleware(handler, message, {"session": session})

    word_row = (
        await session.execute(
            select(WordFrequency).where(
                WordFrequency.chat_id == chat_id,
                WordFrequency.user_id == user_id,
                WordFrequency.word == "привет",
            )
        )
    ).scalar_one()
    assert word_row.count == 2

    emoji_row = (
        await session.execute(
            select(EmojiFrequency).where(
                EmojiFrequency.chat_id == chat_id,
                EmojiFrequency.user_id == user_id,
                EmojiFrequency.emoji == "🔥",
            )
        )
    ).scalar_one()
    assert emoji_row.count == 1


# --- Task 3: правки (D-03, append-only) ----------------------------------


@pytest.mark.asyncio
async def test_save_edit_appends_row_and_does_not_touch_original_text(session):
    """D-03: правка добавляет строку в message_edits, оригинал messages.text
    остаётся нетронутым (никакого UPDATE messages SET text = ...)."""
    chat_id = -100123456804
    user_id = 555000204
    user = User(id=user_id, is_bot=False, first_name="Правщик")

    handler = AsyncMock(return_value="handled")
    middleware = CollectorMiddleware()

    original = _make_message(9001, chat_id, user, text="оригинальный текст")
    await middleware(handler, original, {"session": session})

    saved = await message_service.save_edit(
        session,
        chat_id=chat_id,
        telegram_message_id=9001,
        new_text="отредактированный текст",
    )
    await session.commit()

    assert saved is True

    original_row = (
        await session.execute(
            select(MessageModel).where(
                MessageModel.chat_id == chat_id,
                MessageModel.telegram_message_id == 9001,
            )
        )
    ).scalar_one()
    assert original_row.text == "оригинальный текст"  # оригинал не тронут

    edit_row = (
        await session.execute(
            select(MessageEdit).where(
                MessageEdit.chat_id == chat_id,
                MessageEdit.telegram_message_id == 9001,
            )
        )
    ).scalar_one()
    assert edit_row.new_text == "отредактированный текст"
    assert edit_row.message_id == original_row.id


@pytest.mark.asyncio
async def test_save_edit_on_unknown_message_is_noop(session):
    """Правка на сообщение, которое никогда не было захвачено (например, до
    старта сбора) — пропускается, без ошибки."""
    saved = await message_service.save_edit(
        session,
        chat_id=-100123456805,
        telegram_message_id=999999,
        new_text="неважно",
    )
    assert saved is False
