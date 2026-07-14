"""/ask <вопрос> — RAG-поиск по истории чата (AI-04, D-05/D-06).

Тонкий хендлер: обрезает вопрос до settings.ai_ask_max_query_chars (V5 —
защита от патологического ввода) и зовёт ask_service.answer — вся логика
гибридного поиска, RRF и честного отказа D-05 в сервисе. Ответ (и честный
отказ, и ответ LLM) отправляется как есть, plain text (без parse_mode="HTML",
как и /summary — LLM может эхом вернуть чат-текст с `<`/`>`/`&`).
"""

from __future__ import annotations

import logging

from aiogram import Router
from aiogram.filters import Command
from aiogram.filters import CommandObject
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import settings
from bot.services import ask_service

router = Router()
logger = logging.getLogger(__name__)


@router.message(Command("ask"))
async def ask_command(message: Message, command: CommandObject, session: AsyncSession) -> None:
    if message.from_user is None:
        return

    if not command.args:
        await message.reply("Использование: /ask о чём спорили вчера?")
        return

    question = command.args.strip()[: settings.ai_ask_max_query_chars]  # V5

    try:
        answer_text = await ask_service.answer(session, message.chat.id, question)
    except Exception:  # noqa: BLE001 - обязаны сообщить об ошибке в чат, а не упасть молча
        logger.exception("ask_service.answer упал для chat_id=%s", message.chat.id)
        await message.reply("Не удалось найти ответ — попробуйте позже.")
        return

    await message.answer(answer_text)
