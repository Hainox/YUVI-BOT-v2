"""Общий резолв "цели" пользовательской команды по паре (reply, entities,
текстовый аргумент) — reply > text_mention entity > @username/id-аргумент.

WR-04 (04.2-REVIEW): `_resolve_by_username_or_id`/`_resolve_target` были
byte-for-byte продублированы трижды (`bot/handlers/economy.py::
_resolve_transfer_target`, `bot/handlers/duel.py::_resolve_target`,
`bot/handlers/farm_admin.py::_resolve_target`) — эта функция теперь живёт в
одном месте, а хендлеры импортируют её отсюда вместо переопределения.
"""

from __future__ import annotations

from aiogram.types import Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from common.models.user import User


async def resolve_by_username_or_id(session: AsyncSession, arg: str) -> tuple[int, str] | None:
    """Резолвит `@username` или числовой id через таблицу users (аналог card.py)."""
    if arg.startswith("@"):
        stmt = select(User.id, User.first_name).where(User.username == arg[1:])
    elif arg.lstrip("-").isdigit():
        stmt = select(User.id, User.first_name).where(User.id == int(arg))
    else:
        return None

    row = (await session.execute(stmt)).first()
    if row is None:
        return None
    return row.id, row.first_name or str(row.id)


async def resolve_target(
    message: Message, session: AsyncSession, target_arg: str | None
) -> tuple[int, str] | None:
    """Резолв цели: reply > text_mention entity > @username/id-аргумент."""
    if message.reply_to_message is not None and message.reply_to_message.from_user is not None:
        user = message.reply_to_message.from_user
        return user.id, user.first_name or str(user.id)

    if message.entities:
        for entity in message.entities:
            if entity.type == "text_mention" and entity.user is not None:
                user = entity.user
                return user.id, user.first_name or str(user.id)

    if target_arg is not None:
        return await resolve_by_username_or_id(session, target_arg)

    return None
