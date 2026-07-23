"""Автопост входного сообщения Mini App при старте бота (D-01/D-02/D-03).

При старте проверяет, отправлялось ли уже это сообщение (message_id хранится
в `bot_settings` через `settings_service`, ключ `PINNED_MESSAGE_KEY`); если
да — no-op НАВСЕГДА, без повторной проверки текущего состояния закрепа чата.
Иначе отправляет сообщение с inline URL-кнопкой deep-link
(`t.me/<bot>?startapp=<chat_id>`), пробует закрепить и сохраняет message_id.

Раньше идемпотентность сверялась с `chat.pinned_message` (это ТОЛЬКО самое
верхнее закреплённое сообщение чата, не список всех пинов) — если админ
закреплял что-то своё поверх (или у бота изначально нет права
`can_pin_messages`, как в этом чате — попытка закрепа тихо проваливается),
`chat.pinned_message` переставал совпадать с нашим message_id, и при каждом
рестарте бота сообщение репостилось заново, засоряя чат дублями и/или
вытесняя чужие закрепы. Зафиксировано на живом чате при частых рестартах во
время деплоя. Теперь закреп — best-effort попытка ровно один раз в жизни
чата; дальше сообщение просто существует (закреплённым или нет — не важно),
рестарты бота его больше не трогают.

Вызывается из `bot/main.py::run()` сразу после `setup_bot_commands(bot)`, с
собственной короткой `SessionLocal()`-сессией (форма `scheduler.py::
register`'s "сам открывает SessionLocal").
"""

from __future__ import annotations

import logging

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.exceptions import TelegramForbiddenError
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


def casino_message_content(bot_username: str, chat_id: int) -> tuple[str, InlineKeyboardMarkup]:
    """Текст + inline-кнопка входного сообщения казино.

    Общий контент для ensure_pinned_menu (один раз при старте) и
    /casino-команды (bot/handlers/casino.py, по запросу в любой момент).
    """
    url = f"https://t.me/{bot_username}?startapp={chat_id}"
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=_BUTTON_LABEL, url=url)]]
    )
    return _PINNED_MESSAGE_TEXT, keyboard


async def ensure_pinned_menu(bot: Bot, session: AsyncSession, chat_id: int) -> None:
    """Постит входное сообщение казино ровно один раз за всю историю чата (D-02)."""
    stored_id = await settings_service.get_setting(session, chat_id, PINNED_MESSAGE_KEY, default="")
    if stored_id:
        return  # уже отправляли раньше — больше никогда не трогаем закреп чата

    bot_user = await bot.get_me()
    text, keyboard = casino_message_content(bot_user.username, chat_id)
    message = await bot.send_message(chat_id, text, reply_markup=keyboard, parse_mode="HTML")
    try:
        await bot.pin_chat_message(chat_id, message.message_id, disable_notification=True)
    except (TelegramBadRequest, TelegramForbiddenError):
        logger.warning(
            "ensure_pinned_menu: pin_chat_message failed for chat_id=%s (missing "
            "'Pin Messages' permission?) — message sent but left unpinned, per "
            "docs/botfather-setup.md",
            chat_id,
        )
    await settings_service.set_setting(
        session, chat_id, PINNED_MESSAGE_KEY, str(message.message_id), updated_by_tg_id=bot_user.id
    )
    await session.commit()
