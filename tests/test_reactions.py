"""Интеграционный тест on_reaction / reaction_service против живого Postgres
(фикстура `session` из tests/conftest.py — транзакция-на-тест с rollback).

Доказывает DATA-02 (реакции): реакция на сохранённое сообщение создаёт строку
reactions с внутренним message_id; реакция на несохранённое сообщение и
анонимный актор (event.user is None) не роняют бота; повторный апдейт с другим
набором эмодзи заменяет прежний (current-state snapshot, Assumption A3).
"""

from __future__ import annotations

from datetime import datetime
from datetime import timezone

import pytest
from aiogram.types import Chat
from aiogram.types import MessageReactionUpdated
from aiogram.types import ReactionTypeEmoji
from aiogram.types import User
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from bot.handlers.reactions import on_reaction
from common.models.message import Message as MessageModel
from common.models.reaction import Reaction


async def _insert_message(session, chat_id: int, telegram_message_id: int) -> MessageModel:
    """Вспомогательно вставляет сообщение напрямую (обходя CollectorMiddleware —
    в этом тесте важна только реакция поверх уже сохранённого сообщения)."""
    stmt = pg_insert(MessageModel).values(
        chat_id=chat_id,
        telegram_message_id=telegram_message_id,
        user_id=None,
        text="сообщение для реакции",
        content_type="text",
    )
    stmt = stmt.on_conflict_do_nothing(index_elements=["chat_id", "telegram_message_id"])
    await session.execute(stmt)
    result = await session.execute(
        select(MessageModel).where(
            MessageModel.chat_id == chat_id,
            MessageModel.telegram_message_id == telegram_message_id,
        )
    )
    return result.scalar_one()


def _make_reaction_event(
    chat_id: int,
    message_id: int,
    user: User | None,
    emojis: list[str],
) -> MessageReactionUpdated:
    chat = Chat(id=chat_id, type="group")
    return MessageReactionUpdated(
        chat=chat,
        message_id=message_id,
        date=datetime.now(timezone.utc),
        old_reaction=[],
        new_reaction=[ReactionTypeEmoji(type="emoji", emoji=e) for e in emojis],
        user=user,
        actor_chat=None,
    )


@pytest.mark.asyncio
async def test_reaction_on_saved_message_creates_row_with_internal_message_id(session):
    chat_id = -100987654001
    telegram_message_id = 7001
    saved_message = await _insert_message(session, chat_id, telegram_message_id)

    actor = User(id=888000111, is_bot=False, first_name="Реагирующий")
    event = _make_reaction_event(chat_id, telegram_message_id, actor, ["👍"])

    await on_reaction(event, session)

    row = (
        await session.execute(
            select(Reaction).where(
                Reaction.message_id == saved_message.id,
                Reaction.actor_user_id == actor.id,
            )
        )
    ).scalar_one()
    assert row.emoji == "👍"
    assert row.message_id == saved_message.id


@pytest.mark.asyncio
async def test_reaction_on_unsaved_message_is_skipped_without_exception(session):
    chat_id = -100987654002
    actor = User(id=888000112, is_bot=False, first_name="Реагирующий2")
    event = _make_reaction_event(chat_id, message_id=9999, user=actor, emojis=["🔥"])

    await on_reaction(event, session)  # не должно бросить исключение

    count = (
        await session.execute(select(Reaction).where(Reaction.actor_user_id == actor.id))
    ).scalars().all()
    assert count == []


@pytest.mark.asyncio
async def test_reaction_with_anonymous_actor_has_null_actor_user_id(session):
    chat_id = -100987654003
    telegram_message_id = 7003
    saved_message = await _insert_message(session, chat_id, telegram_message_id)

    event = _make_reaction_event(chat_id, telegram_message_id, user=None, emojis=["❤️"])

    await on_reaction(event, session)  # не должно бросить AttributeError

    row = (
        await session.execute(
            select(Reaction).where(
                Reaction.message_id == saved_message.id,
                Reaction.actor_user_id.is_(None),
            )
        )
    ).scalar_one()
    assert row.emoji == "❤️"


@pytest.mark.asyncio
async def test_repeated_reaction_update_replaces_previous_emoji_set(session):
    chat_id = -100987654004
    telegram_message_id = 7004
    saved_message = await _insert_message(session, chat_id, telegram_message_id)
    actor = User(id=888000114, is_bot=False, first_name="Реагирующий4")

    first_event = _make_reaction_event(chat_id, telegram_message_id, actor, ["👍"])
    await on_reaction(first_event, session)

    second_event = _make_reaction_event(chat_id, telegram_message_id, actor, ["😂", "🔥"])
    await on_reaction(second_event, session)

    rows = (
        (
            await session.execute(
                select(Reaction).where(
                    Reaction.message_id == saved_message.id,
                    Reaction.actor_user_id == actor.id,
                )
            )
        )
        .scalars()
        .all()
    )
    emojis = {row.emoji for row in rows}
    assert emojis == {"😂", "🔥"}
    assert "👍" not in emojis
