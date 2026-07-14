"""/topics /phrase /joke — темы обсуждений, фраза дня, анекдот (AI-05).

D-07: строго по явному вызову участника — этот модуль НЕ регистрирует
никаких фоновых задач планировщика или автопоста, несмотря на слово "дня" в
паре названий команд. Тонкие хендлеры: парсят вход, зовут
topics_service/phrase_service/joke_service, экранируют LLM-вывод через
html.escape() перед parse_mode="HTML" (T-02-15 — LLM может вернуть в тексте
`<`/`>`/`&`, доверять этому нельзя).
"""

from __future__ import annotations

import html
import logging

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.services import joke_service
from bot.services import phrase_service
from bot.services import topics_service

router = Router()
logger = logging.getLogger(__name__)


@router.message(Command("topics"))
async def topics_command(message: Message, session: AsyncSession) -> None:
    if message.from_user is None:
        return

    try:
        text = await topics_service.build_topics(session, message.chat.id)
    except Exception:  # noqa: BLE001 - хендлер обязан сообщить об ошибке в чат, а не упасть молча
        logger.exception("build_topics упал для chat_id=%s", message.chat.id)
        await message.reply("Не удалось выделить темы — попробуйте позже.")
        return

    await message.answer(f"<b>Темы обсуждений</b>\n{html.escape(text)}", parse_mode="HTML")


@router.message(Command("phrase"))
async def phrase_command(message: Message, session: AsyncSession) -> None:
    if message.from_user is None:
        return

    try:
        text = await phrase_service.build_phrase(session, message.chat.id)
    except Exception:  # noqa: BLE001 - хендлер обязан сообщить об ошибке в чат, а не упасть молча
        logger.exception("build_phrase упал для chat_id=%s", message.chat.id)
        await message.reply("Не удалось выбрать фразу дня — попробуйте позже.")
        return

    await message.answer(f"<b>Фраза дня</b>\n{html.escape(text)}", parse_mode="HTML")


@router.message(Command("joke"))
async def joke_command(message: Message, session: AsyncSession) -> None:
    if message.from_user is None:
        return

    try:
        text = await joke_service.build_joke(session, message.chat.id)
    except Exception:  # noqa: BLE001 - хендлер обязан сообщить об ошибке в чат, а не упасть молча
        logger.exception("build_joke упал для chat_id=%s", message.chat.id)
        await message.reply("Не удалось сочинить анекдот — попробуйте позже.")
        return

    await message.answer(f"<b>Анекдот дня</b>\n{html.escape(text)}", parse_mode="HTML")
