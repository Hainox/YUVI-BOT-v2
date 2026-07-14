"""/digest N — ручной запуск дайджеста (AI-02, D-02).

Тонкий хендлер: парсит N (дней), зовёт digest_service.build_manual_digest и
отвечает готовым текстом. В отличие от автодайджеста (D-01/D-03) ручной
вызов НЕ подавляется порогом digest_min_messages — участник явно попросил
дайджест, спамить нечем. Вывод — plain text (без parse_mode="HTML"): дайджест
включает AI-пересказ, а модель может эхом вернуть `<`/`>`/`&` из чата
(RESEARCH.md Anti-Patterns, T-02-15) — оборачивать сырой LLM-вывод в HTML
небезопасно.
"""

from __future__ import annotations

import logging

from aiogram import Router
from aiogram.filters import Command
from aiogram.filters import CommandObject
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.services import digest_service

router = Router()
logger = logging.getLogger(__name__)

DEFAULT_DIGEST_DAYS = 1


def _parse_days_arg(command: CommandObject) -> int:
    """Парсит необязательный числовой аргумент `/digest N` (дефолт 1 день)."""
    if command.args is None:
        return DEFAULT_DIGEST_DAYS
    arg = command.args.strip()
    if not arg.isdigit():
        return DEFAULT_DIGEST_DAYS
    return int(arg)


@router.message(Command("digest"))
async def digest_command(message: Message, command: CommandObject, session: AsyncSession) -> None:
    if message.from_user is None:
        return

    days = _parse_days_arg(command)
    try:
        text = await digest_service.build_manual_digest(session, message.chat.id, days)
    except Exception:  # noqa: BLE001 - хендлер обязан сообщить об ошибке в чат, а не упасть молча
        logger.exception("build_manual_digest упал для chat_id=%s", message.chat.id)
        await message.reply("Не удалось собрать дайджест — попробуйте позже.")
        return

    await message.answer(text)  # без parse_mode: пересказ может эхом вернуть HTML-подобный текст (T-02-15)
