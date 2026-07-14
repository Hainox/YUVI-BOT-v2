"""Юнит-тесты admin_service.is_chat_admin и ChatAdminFilter (D-02).

Мокает bot.get_chat_member (AsyncMock) — без реального Telegram API и без БД.
Доказывает: владелец/админ -> True, обычный участник -> False, ChatAdminFilter
не падает при from_user is None (анонимный пост).
"""

from __future__ import annotations

from datetime import datetime
from datetime import timezone
from unittest.mock import AsyncMock

import pytest
from aiogram.types import Chat
from aiogram.types import ChatMemberAdministrator
from aiogram.types import ChatMemberMember
from aiogram.types import ChatMemberOwner
from aiogram.types import Message
from aiogram.types import User

from bot.filters.chat_admin import ChatAdminFilter
from bot.services import admin_service


def _admin_member(user: User) -> ChatMemberAdministrator:
    return ChatMemberAdministrator(
        user=user,
        can_be_edited=False,
        is_anonymous=False,
        can_manage_chat=True,
        can_delete_messages=True,
        can_manage_video_chats=True,
        can_restrict_members=True,
        can_promote_members=False,
        can_change_info=True,
        can_invite_users=True,
        can_post_stories=False,
        can_edit_stories=False,
        can_delete_stories=False,
    )


def _owner_member(user: User) -> ChatMemberOwner:
    return ChatMemberOwner(user=user, is_anonymous=False)


def _regular_member(user: User) -> ChatMemberMember:
    return ChatMemberMember(user=user)


@pytest.mark.asyncio
async def test_is_chat_admin_true_for_administrator():
    user = User(id=111, is_bot=False, first_name="Админ")
    bot = AsyncMock()
    bot.get_chat_member.return_value = _admin_member(user)

    result = await admin_service.is_chat_admin(bot, chat_id=-100123, user_id=111)

    assert result is True
    bot.get_chat_member.assert_awaited_once_with(-100123, 111)


@pytest.mark.asyncio
async def test_is_chat_admin_true_for_owner():
    user = User(id=222, is_bot=False, first_name="Владелец")
    bot = AsyncMock()
    bot.get_chat_member.return_value = _owner_member(user)

    result = await admin_service.is_chat_admin(bot, chat_id=-100123, user_id=222)

    assert result is True


@pytest.mark.asyncio
async def test_is_chat_admin_false_for_regular_member():
    user = User(id=333, is_bot=False, first_name="Участник")
    bot = AsyncMock()
    bot.get_chat_member.return_value = _regular_member(user)

    result = await admin_service.is_chat_admin(bot, chat_id=-100123, user_id=333)

    assert result is False


@pytest.mark.asyncio
async def test_is_chat_admin_calls_get_chat_member_live_every_time():
    """D-02: статус не кэшируется — каждый вызов делает новый запрос к Telegram."""
    user = User(id=444, is_bot=False, first_name="Участник")
    bot = AsyncMock()
    bot.get_chat_member.return_value = _regular_member(user)

    await admin_service.is_chat_admin(bot, chat_id=-100999, user_id=444)
    await admin_service.is_chat_admin(bot, chat_id=-100999, user_id=444)

    assert bot.get_chat_member.await_count == 2


@pytest.mark.asyncio
async def test_chat_admin_filter_true_for_admin():
    user = User(id=555, is_bot=False, first_name="Админ")
    chat = Chat(id=-100777, type="group")
    message = Message(
        message_id=1,
        date=datetime.now(timezone.utc),
        chat=chat,
        from_user=user,
        text="/backfill",
    )
    bot = AsyncMock()
    bot.get_chat_member.return_value = _admin_member(user)

    result = await ChatAdminFilter()(message, bot)

    assert result is True


@pytest.mark.asyncio
async def test_chat_admin_filter_false_for_regular_member():
    user = User(id=666, is_bot=False, first_name="Участник")
    chat = Chat(id=-100777, type="group")
    message = Message(
        message_id=2,
        date=datetime.now(timezone.utc),
        chat=chat,
        from_user=user,
        text="/backfill",
    )
    bot = AsyncMock()
    bot.get_chat_member.return_value = _regular_member(user)

    result = await ChatAdminFilter()(message, bot)

    assert result is False


@pytest.mark.asyncio
async def test_chat_admin_filter_false_when_from_user_is_none():
    """Анонимный пост (from_user is None) — фильтр не падает, возвращает False."""
    chat = Chat(id=-100777, type="group")
    message = Message(message_id=3, date=datetime.now(timezone.utc), chat=chat, from_user=None, text="/backfill")
    bot = AsyncMock()

    result = await ChatAdminFilter()(message, bot)

    assert result is False
    bot.get_chat_member.assert_not_awaited()
