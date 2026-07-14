"""Update-level middleware: открывает AsyncSession и кладёт её в data["session"].

Регистрируется как dp.update.outer_middleware() — срабатывает для КАЖДОГО
типа апдейта (message, edited_message, message_reaction, chat_member, ...),
до любых фильтров/роутеров.
"""

from __future__ import annotations

from collections.abc import Awaitable
from collections.abc import Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject

from common.db.session import SessionLocal


class DbSessionMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        async with SessionLocal() as session:
            data["session"] = session
            return await handler(event, data)
