"""Message-level middleware: DATA-01 workhorse — пишет 100% сообщений до роутинга.

Регистрируется как dp.message.outer_middleware() (НЕ catch-all @router.message() —
такой подход теряет команды, см. 01-RESEARCH.md Anti-Patterns). ВСЕГДА вызывает
next handler, даже если запись пропущена или упала — команды/прочие хендлеры
никогда не блокируются этим middleware (DATA-01).
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable
from collections.abc import Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.services import message_service

logger = logging.getLogger(__name__)


class CollectorMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[Message, dict[str, Any]], Awaitable[Any]],
        event: Message,
        data: dict[str, Any],
    ) -> Any:
        session: AsyncSession = data["session"]

        if event.from_user is None or event.from_user.is_bot:
            # Анонимный админ чата / linked-channel пост / сам бот (Pitfall 5) —
            # запись пропускаем, но роутинг НЕ блокируем.
            return await handler(event, data)

        # media/frequency: см. план 04 — на этом срезе пишем только текст+reply+thread.
        try:
            await message_service.save_message(session, event)
            await session.commit()
        except Exception:
            await session.rollback()
            logger.exception(
                "CollectorMiddleware: не удалось записать сообщение chat_id=%s message_id=%s",
                event.chat.id,
                event.message_id,
            )

        return await handler(event, data)
