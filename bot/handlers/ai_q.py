"""`/q <вопрос>` и ответы только на прямое обращение к боту.

Свободный текст без @упоминания бота и без реплая на его сообщение здесь
пропускается дальше по цепочке роутеров. Явная команда `/q` остаётся удобным
коротким способом вызвать тот же поиск.
"""

from __future__ import annotations

import logging
import re

from aiogram import Bot
from aiogram import F
from aiogram import Router
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.filters import Command
from aiogram.filters import CommandObject
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import settings
from bot.services import q_service

router = Router()
logger = logging.getLogger(__name__)


async def _bot_is_addressed(message: Message, bot: Bot) -> bool:
    bot_user = await bot.get_me()
    if (
        message.reply_to_message is not None
        and message.reply_to_message.from_user is not None
        and message.reply_to_message.from_user.id == bot_user.id
    ):
        return True

    if not message.entities or message.text is None:
        return False
    for entity in message.entities:
        if entity.type == "text_mention" and entity.user is not None:
            if entity.user.id == bot_user.id:
                return True
        elif entity.type == "mention":
            mentioned = message.text[entity.offset : entity.offset + entity.length].lstrip("@")
            if bot_user.username and mentioned.casefold() == bot_user.username.casefold():
                return True
    return False


def _without_bot_mention(message: Message, bot_username: str | None) -> str:
    text = message.text or ""
    if bot_username:
        text = re.sub(rf"@{re.escape(bot_username)}\b", "", text, flags=re.IGNORECASE)
    return text.strip(" \t\n,:;—-")


async def _answer(message: Message, question: str, session: AsyncSession) -> None:
    if not question:
        await message.reply("Использование: /q о чём спорили вчера?")
        return
    try:
        answer_text = await q_service.answer(
            session, message.chat.id, question[: settings.ai_q_max_query_chars]
        )
    except Exception:  # noqa: BLE001 - ошибка AI не должна ронять polling
        logger.exception("q_service.answer упал для chat_id=%s", message.chat.id)
        await message.reply("Не удалось найти ответ — попробуйте позже.")
        return
    await message.answer(answer_text)


@router.message(Command("q"))
async def q_command(message: Message, command: CommandObject, session: AsyncSession) -> None:
    await _answer(message, (command.args or "").strip(), session)


@router.message(F.text)
async def addressed_text(message: Message, bot: Bot, session: AsyncSession) -> None:
    if message.text is None or message.text.startswith("/"):
        raise SkipHandler
    if not await _bot_is_addressed(message, bot):
        raise SkipHandler
    bot_user = await bot.get_me()
    await _answer(message, _without_bot_mention(message, bot_user.username), session)
