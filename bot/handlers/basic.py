from __future__ import annotations

from aiogram import Bot
from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from bot.config import settings
from bot.services.pinned_menu_service import casino_message_content

router = Router()


@router.message(Command("start"))
async def start_command(message: Message, bot: Bot) -> None:
    bot_user = await bot.get_me()
    text, keyboard = casino_message_content(bot_user.username, settings.chat_id)
    await message.answer(
        "Привет! Это Yuvi Bot v2 🎲\n"
        "Статистика чата, AI-команды, экономика и казино — всё в одном боте.\n"
        "Список команд — через «/» в чате.",
    )
    await message.answer(text, reply_markup=keyboard, parse_mode="HTML")


@router.message(Command("health"))
async def health_command(message: Message) -> None:
    await message.answer("OK: bot is running")

