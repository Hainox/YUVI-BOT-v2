"""ChatAdminFilter — aiogram BaseFilter, оборачивающий admin_service.is_chat_admin.

Live-проверка прав на каждый апдейт (D-02) — не кэшируется. Используется там,
где нужен декларативный фильтр вместо ручной проверки внутри хендлера.
"""

from __future__ import annotations

from aiogram import Bot
from aiogram.filters import BaseFilter
from aiogram.types import Message

from bot.services import admin_service


class ChatAdminFilter(BaseFilter):
    async def __call__(self, message: Message, bot: Bot) -> bool:
        return message.from_user is not None and await admin_service.is_chat_admin(
            bot, message.chat.id, message.from_user.id
        )
