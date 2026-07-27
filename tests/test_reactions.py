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
from bot.services import reaction_service
from common.models.message import Message as MessageModel
from common.models.reaction import Reaction
from common.models.user import User as UserModel


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


# --- get_top_reactions_received / get_top_reactions_given (карточка /card) ---


async def _ensure_user(session, user_id: int, first_name: str = "Тест") -> None:
    session.add(UserModel(id=user_id, first_name=first_name))
    await session.flush()


async def _insert_authored_message(
    session, chat_id: int, telegram_message_id: int, author_id: int
) -> MessageModel:
    stmt = pg_insert(MessageModel).values(
        chat_id=chat_id,
        telegram_message_id=telegram_message_id,
        user_id=author_id,
        text="сообщение",
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


@pytest.mark.asyncio
async def test_get_top_reactions_received_counts_reactions_on_own_messages(session):
    """received = реакции на сообщения, АВТОР которых — user_id (не важно, кто
    их поставил)."""
    chat_id = -100987654005
    author_id, other_author_id, actor_id = 888100001, 888100002, 888100003
    await _ensure_user(session, author_id, "Автор")
    await _ensure_user(session, other_author_id, "Другой автор")
    await _ensure_user(session, actor_id, "Реагирующий")

    own_message = await _insert_authored_message(session, chat_id, 8001, author_id)
    other_message = await _insert_authored_message(session, chat_id, 8002, other_author_id)

    actor = User(id=actor_id, is_bot=False, first_name="Реагирующий")
    await on_reaction(
        _make_reaction_event(chat_id, own_message.telegram_message_id, actor, ["❤️", "🔥"]),
        session,
    )
    # Реакция на ЧУЖОЕ сообщение не должна попасть в "полученные" author_id.
    await on_reaction(
        _make_reaction_event(chat_id, other_message.telegram_message_id, actor, ["😁"]),
        session,
    )

    received = await reaction_service.get_top_reactions_received(session, chat_id, author_id)

    assert {row["emoji"] for row in received} == {"❤️", "🔥"}
    assert all(row["count"] == 1 for row in received)


@pytest.mark.asyncio
async def test_get_top_reactions_given_counts_reactions_placed_by_user(session):
    """given = реакции, которые user_id САМ поставил на чужие сообщения."""
    chat_id = -100987654006
    author_id, actor_id, other_actor_id = 888100004, 888100005, 888100006
    await _ensure_user(session, author_id, "Автор")
    await _ensure_user(session, actor_id, "Ставящий")
    await _ensure_user(session, other_actor_id, "Другой ставящий")

    message = await _insert_authored_message(session, chat_id, 8003, author_id)

    actor = User(id=actor_id, is_bot=False, first_name="Ставящий")
    other_actor = User(id=other_actor_id, is_bot=False, first_name="Другой")
    await on_reaction(
        _make_reaction_event(chat_id, message.telegram_message_id, actor, ["🤣"]),
        session,
    )
    # Реакция ДРУГОГО актора не должна попасть в "поставленные" actor_id.
    await on_reaction(
        _make_reaction_event(chat_id, message.telegram_message_id, other_actor, ["💔"]),
        session,
    )

    given = await reaction_service.get_top_reactions_given(session, chat_id, actor_id)

    assert given == [{"emoji": "🤣", "count": 1}]


@pytest.mark.asyncio
async def test_get_top_reactions_empty_for_user_with_no_reactions(session):
    chat_id = -100987654007
    user_id = 888100007
    await _ensure_user(session, user_id, "Молчун")

    assert await reaction_service.get_top_reactions_received(session, chat_id, user_id) == []
    assert await reaction_service.get_top_reactions_given(session, chat_id, user_id) == []
