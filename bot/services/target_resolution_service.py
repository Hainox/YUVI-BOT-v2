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

from bot.constants import TELEGRAM_SERVICE_ACCOUNT_ID
from common.models.user import User


def _is_valid_target(user_id: int, is_bot: bool) -> bool:
    """False для служебного аккаунта Telegram (777000, привязанный канал —
    is_bot=False на проводе, поэтому обычная is_bot-проверка его не ловит)
    и для любого bot-аккаунта (включая сам YUVI_BOT) — ни один не должен
    резолвиться целью дуэли/grant/transfer."""
    return not is_bot and user_id != TELEGRAM_SERVICE_ACCOUNT_ID


async def resolve_by_username_or_id(session: AsyncSession, arg: str) -> tuple[int, str] | None:
    """Резолвит `@username` или числовой id через таблицу users (аналог card.py)."""
    if arg.startswith("@"):
        stmt = select(User.id, User.first_name).where(User.username == arg[1:])
    elif arg.lstrip("-").isdigit():
        stmt = select(User.id, User.first_name).where(User.id == int(arg))
    else:
        return None

    row = (await session.execute(stmt)).first()
    if row is None or row.id == TELEGRAM_SERVICE_ACCOUNT_ID:
        return None
    return row.id, row.first_name or str(row.id)


async def resolve_target(
    message: Message, session: AsyncSession, target_arg: str | None
) -> tuple[int, str] | None:
    """Резолв цели: reply > text_mention entity > @username/id-аргумент.
    Служебный аккаунт Telegram и любой bot-аккаунт не могут быть целью —
    невалидный кандидат падает дальше по цепочке (не сразу None), т.к.
    дальше может найтись валидная цель (напр. reply на пост бота +
    отдельный @username-аргумент в том же сообщении)."""
    if message.reply_to_message is not None and message.reply_to_message.from_user is not None:
        user = message.reply_to_message.from_user
        if _is_valid_target(user.id, user.is_bot):
            return user.id, user.first_name or str(user.id)

    if message.entities:
        for entity in message.entities:
            if entity.type == "text_mention" and entity.user is not None:
                user = entity.user
                if _is_valid_target(user.id, user.is_bot):
                    return user.id, user.first_name or str(user.id)

    if target_arg is not None:
        return await resolve_by_username_or_id(session, target_arg)

    return None
