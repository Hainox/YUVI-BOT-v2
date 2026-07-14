"""Статистические команды (STATS-01). Тонкий хендлер: парсит вход, зовёт
stats_service, форматирует и отвечает — вся логика в сервисе.
"""

from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.services import stats_service

router = Router()


def _parse_days_arg(message: Message) -> int | None:
    """Парсит необязательный числовой аргумент `/chatstats N` (D-06)."""
    if message.text is None:
        return None
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        return None
    arg = parts[1].strip()
    if not arg.isdigit():
        return None
    return int(arg)


@router.message(Command("chatstats"))
async def chatstats_command(message: Message, session: AsyncSession) -> None:
    if message.from_user is None:
        return

    days = _parse_days_arg(message)
    total = await stats_service.get_chat_message_count(session, message.chat.id, days)

    if days is not None:
        text = f"Сообщений в чате за последние {days} дн.: {total}"
    else:
        text = f"Сообщений в чате за всё время: {total}"

    await message.answer(text)  # D-05: всегда отвечаем прямо в группе
