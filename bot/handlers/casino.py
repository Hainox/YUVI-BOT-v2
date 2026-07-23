"""Команда /casino — открыть Mini App казино по запросу (CASINO-04).

Раньше кнопка на Mini App отправлялась ТОЛЬКО один раз при старте бота
(pinned_menu_service.ensure_pinned_menu) — без отдельной команды участники
не могли получить её повторно, если сообщение уходило вниз под новыми
сообщениями (обнаружено на живом чате: команда даже не значилась в списке
"/"). /casino переиспользует тот же текст/кнопку (pinned_menu_service.
casino_message_content) и отправляет их по запросу, сколько угодно раз.

Deep-link всегда указывает на settings.chat_id (единственный отслеживаемый
чат бота, см. PROJECT.md) независимо от того, откуда вызвана команда — это
позволяет открыть Mini App даже из личных сообщений с ботом.
"""

from __future__ import annotations

from aiogram import Bot
from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from bot.config import settings
from bot.services.pinned_menu_service import casino_message_content

router = Router()


@router.message(Command("casino"))
async def casino_command(message: Message, bot: Bot) -> None:
    bot_user = await bot.get_me()
    text, keyboard = casino_message_content(bot_user.username, settings.chat_id)
    await message.answer(text, reply_markup=keyboard, parse_mode="HTML")
