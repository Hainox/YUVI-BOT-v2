"""/summary /sum /summary_custom /sumc — AI-пересказ переписки (AI-01).

Тонкий хендлер: парсит вход -> summary_service собирает контекст и промпт ->
stream_edit доставляет стриминговый ответ в ОДНО сообщение (AI-06). Никакой
SQL/бизнес-логики здесь — всё в bot/services/summary_service.py. Вывод LLM —
plain text (без parse_mode="HTML"): модель может эхом вернуть чат-текст с
`<`/`>`/`&`, оборачивать его в HTML небезопасно (RESEARCH.md Anti-Patterns,
T-02-15).
"""

from __future__ import annotations

import logging

from aiogram import Router
from aiogram.filters import Command
from aiogram.filters import CommandObject
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import settings
from bot.services import summary_service
from bot.utils.stream_edit import stream_into_message

router = Router()
logger = logging.getLogger(__name__)

DEFAULT_SUMMARY_N = 100


def _parse_n_arg(command: CommandObject) -> int:
    """Парсит необязательный числовой аргумент `/summary N` (дефолт 100)."""
    if command.args is None:
        return DEFAULT_SUMMARY_N
    arg = command.args.strip()
    if not arg.isdigit():
        return DEFAULT_SUMMARY_N
    return int(arg)


async def _stream_and_report_errors(message: Message, session: AsyncSession, n: int, focus: str | None) -> None:
    try:
        await stream_into_message(message, summary_service.stream_summary(session, message.chat.id, n, focus))
    except Exception:  # noqa: BLE001 - стрим обязан сообщить об ошибке в чат, а не упасть молча
        logger.exception("stream_summary упал для chat_id=%s", message.chat.id)
        await message.reply("Не удалось получить пересказ — попробуйте позже.")


@router.message(Command("summary", "sum"))
async def summary_command(message: Message, command: CommandObject, session: AsyncSession) -> None:
    if message.from_user is None:
        return

    n = _parse_n_arg(command)
    await _stream_and_report_errors(message, session, n, None)


@router.message(Command("summary_custom", "sumc"))
async def summary_custom_command(message: Message, command: CommandObject, session: AsyncSession) -> None:
    if message.from_user is None:
        return

    if not command.args:
        await message.reply("Использование: /sumc N | фокус (например: /sumc 50 | про котов)")
        return

    n_part, sep, focus_part = command.args.partition("|")
    n_part = n_part.strip()
    focus = focus_part.strip()
    if not n_part.isdigit() or not sep or not focus:
        await message.reply("Использование: /sumc N | фокус")
        return

    n = int(n_part)
    focus = focus[: settings.ai_max_custom_prompt_chars]

    await _stream_and_report_errors(message, session, n, focus)
