"""Автозакреп входного сообщения Mini App при старте бота (D-01/D-02/D-03).

При старте проверяет, есть ли уже своё закреплённое сообщение (message_id
хранится в `bot_settings` через `settings_service`, ключ `PINNED_MESSAGE_KEY`);
если найдено и всё ещё реально закреплено этим же сообщением — no-op
(идемпотентно, D-02). Иначе отправляет сообщение с inline URL-кнопкой
deep-link (`t.me/<bot>?startapp=<chat_id>`), закрепляет его и сохраняет
новый message_id.

Вызывается из `bot/main.py::run()` сразу после `setup_bot_commands(bot)`, с
собственной короткой `SessionLocal()`-сессией (форма `scheduler.py::
register`'s "сам открывает SessionLocal").
"""

from __future__ import annotations

import logging

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import InlineKeyboardButton
from aiogram.types import InlineKeyboardMarkup
from sqlalchemy.ext.asyncio import AsyncSession

from bot.services import settings_service

logger = logging.getLogger(__name__)

PINNED_MESSAGE_KEY = "casino_pinned_message_id"

_PINNED_MESSAGE_TEXT = (
    "🎰 <b>Yuvi скам — казино чата</b>\n"
    "Всё, что можно просадить (или наварить) прямо в Telegram: слоты · рулетка · "
    "блэкджек · кости · монетка, ферма-кликер, гача-баннер, дуэли на ставки, "
    "рынки предсказаний — плюс лидерборд, портфолио, история и переводы.\n"
    "Жми кнопку — открывается прямо в чате, никуда переходить не надо."
)
_BUTTON_LABEL = "🎰 Открыть казино"


async def ensure_pinned_menu(bot: Bot, session: AsyncSession, chat_id: int) -> None:
    """Постит и закрепляет входное сообщение казино ровно один раз (D-02)."""
    stored_id = await settings_service.get_setting(session, chat_id, PINNED_MESSAGE_KEY, default="")
    if stored_id:
        try:
            chat = await bot.get_chat(chat_id)
            if chat.pinned_message is not None and chat.pinned_message.message_id == int(stored_id):
                return  # уже закреплено этим же сообщением — не постим повторно
        except TelegramBadRequest:
            logger.info(
                "ensure_pinned_menu: getChat(%s) не удался (сообщение удалено/чат "
                "недоступен) — постим заново",
                chat_id,
            )

    bot_user = await bot.get_me()
    url = f"https://t.me/{bot_user.username}?startapp={chat_id}"
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=_BUTTON_LABEL, url=url)]]
    )
    message = await bot.send_message(
        chat_id, _PINNED_MESSAGE_TEXT, reply_markup=keyboard, parse_mode="HTML"
    )
    await bot.pin_chat_message(chat_id, message.message_id, disable_notification=True)
    await settings_service.set_setting(
        session, chat_id, PINNED_MESSAGE_KEY, str(message.message_id), updated_by_tg_id=bot_user.id
    )
    await session.commit()
