from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

router = Router()


@router.message(Command("start"))
async def start_command(message: Message) -> None:
    await message.answer(
        "Привет! Это Yuvi Bot v2.\n"
        "Базовый каркас запущен. Следующий шаг — подключение сбора сообщений, статистики и AI-команд."
    )


@router.message(Command("health"))
async def health_command(message: Message) -> None:
    await message.answer("OK: bot is running")

