"""Роутер реакций — @router.message_reaction(), а не хендлер членства чата
(баг эталона: реакции были повешены не на тот тип апдейта и никогда не срабатывали).

Хендлер тонкий: только парсинг события Telegram + один вызов reaction_service +
commit. Никакой SQL-логики внутри (Pattern 2, PATTERNS.md).
"""

from __future__ import annotations

from aiogram import Router
from aiogram.types import MessageReactionUpdated
from sqlalchemy.ext.asyncio import AsyncSession

from bot.services import reaction_service

router = Router()


@router.message_reaction()
async def on_reaction(event: MessageReactionUpdated, session: AsyncSession) -> None:
    # Pitfall 5: event.user is None для анонимного админа/linked-channel поста —
    # actor_user_id остаётся NULL, без AttributeError.
    actor = event.user
    actor_tg_id = actor.id if actor else None
    actor_username = actor.username if actor else None
    actor_first_name = actor.first_name if actor else None

    emojis = [
        reaction.emoji
        for reaction in event.new_reaction
        if getattr(reaction, "type", None) == "emoji"
    ]

    saved = await reaction_service.save_reaction(
        session,
        chat_id=event.chat.id,
        telegram_message_id=event.message_id,
        actor_tg_id=actor_tg_id,
        emojis=emojis,
        actor_username=actor_username,
        actor_first_name=actor_first_name,
    )
    if saved:
        await session.commit()
