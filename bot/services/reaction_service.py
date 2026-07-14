"""Сервис реакций: резолвит внутренний message_id (chat_id, telegram_message_id) ->
messages.id ДО записи (не пишет сырые Telegram id во внешние ключи — баг эталона,
см. RESEARCH.md Anti-Pattern "Storing raw Telegram IDs as foreign keys").

Реакции хранятся как current-state snapshot на (message, actor) — Assumption A3:
каждый апдейт заменяет набор эмодзи актора на текущий (DELETE старых + INSERT новых),
а не добавляет строки в append-only лог.

Вызывается из bot/handlers/reactions.py. commit делает вызывающий хендлер.
"""

from __future__ import annotations

from sqlalchemy import delete
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from common.models.message import Message
from common.models.reaction import Reaction
from common.models.user import User


async def get_message_by_tg_id(
    session: AsyncSession, chat_id: int, telegram_message_id: int
) -> Message | None:
    """Резолвит внутренний Message по (chat_id, telegram_message_id) или None,
    если сообщение ещё не было захвачено (например, реакция на несохранённое
    сообщение до backfill)."""
    result = await session.execute(
        select(Message).where(
            Message.chat_id == chat_id,
            Message.telegram_message_id == telegram_message_id,
        )
    )
    return result.scalar_one_or_none()


async def save_reaction(
    session: AsyncSession,
    chat_id: int,
    telegram_message_id: int,
    actor_tg_id: int | None,
    emojis: list[str],
    actor_username: str | None = None,
    actor_first_name: str | None = None,
) -> bool:
    """Резолвит внутренний message_id и пишет current-state snapshot реакций
    актора на это сообщение.

    Возвращает False без записи, если сообщение ещё не сохранено (вызывающий
    хендлер пропускает апдейт, не падает). actor_user_id остаётся NULL, если
    actor_tg_id is None (анонимный админ, event.user отсутствует).
    """
    message = await get_message_by_tg_id(session, chat_id, telegram_message_id)
    if message is None:
        return False

    if actor_tg_id is not None:
        # Rule 2: актор мог никогда не отправлять сообщений в этот чат (только
        # реагирует) — без upsert строки users не было бы, и FK
        # reactions.actor_user_id -> users.id падал бы IntegrityError. Пишем
        # минимально необходимую строку, ON CONFLICT DO NOTHING — не затираем
        # уже известные данные пользователя, если он раньше писал сообщения.
        user_stmt = pg_insert(User).values(
            id=actor_tg_id,
            username=actor_username,
            first_name=actor_first_name or "",
        )
        user_stmt = user_stmt.on_conflict_do_nothing(index_elements=["id"])
        await session.execute(user_stmt)

    await session.execute(
        delete(Reaction).where(
            Reaction.message_id == message.id,
            Reaction.actor_user_id == actor_tg_id,
        )
    )

    if emojis:
        rows = [
            {"message_id": message.id, "actor_user_id": actor_tg_id, "emoji": emoji}
            for emoji in dict.fromkeys(emojis)  # дедуп с сохранением порядка
        ]
        insert_stmt = pg_insert(Reaction).values(rows)
        insert_stmt = insert_stmt.on_conflict_do_nothing(
            index_elements=["message_id", "actor_user_id", "emoji"],
        )
        await session.execute(insert_stmt)

    return True
