"""Admin-рубильник фичи скачивания медиа (cobalt) целиком — по запросу
владельца бота (2026-07-30), временно выключить именно эту фичу, не трогая
остальной функционал бота.

Та же форма, что bot/handlers/daily_twin_admin.py: персистентно через
bot_settings/settings_service (та же KV, что и /model_set), переживает
рестарт бота, эффект — на следующем же сообщении со ссылкой, без передеплоя.
Гейт — ChatAdminFilter, тот же live-механизм, что и у /daily_twin_off.
"""

from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.filters.chat_admin import ChatAdminFilter
from bot.services import media_dl_service

router = Router()
router.message.filter(ChatAdminFilter())


@router.message(Command("media_dl_off"))
async def media_dl_off_command(message: Message, session: AsyncSession) -> None:
    if message.from_user is None:
        return

    await media_dl_service.set_enabled(session, message.chat.id, False, message.from_user.id)
    await session.commit()
    await message.answer(
        "Скачивание медиа (TikTok/Reels/Shorts) выключено целиком. Остальные "
        "функции бота не затронуты. Включить обратно — /media_dl_on."
    )


@router.message(Command("media_dl_on"))
async def media_dl_on_command(message: Message, session: AsyncSession) -> None:
    if message.from_user is None:
        return

    await media_dl_service.set_enabled(session, message.chat.id, True, message.from_user.id)
    await session.commit()
    await message.answer("Скачивание медиа снова включено.")
