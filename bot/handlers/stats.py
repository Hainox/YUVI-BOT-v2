"""Роутер статистических команд (STATS-01). /chatstats наполняется в Task 2 этого плана."""

from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

router = Router()


@router.message(Command("chatstats"))
async def chatstats_command(message: Message) -> None:
    # Заглушка — реальное чтение stats_service.get_chat_message_count добавляется в Task 2.
    await message.answer("Статистика чата скоро будет доступна.")
