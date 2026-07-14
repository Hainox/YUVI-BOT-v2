"""Сервис проверки прав администратора чата (D-02).

is_chat_admin делает LIVE-запрос bot.get_chat_member на каждый вызов — статус
НЕ кэшируется и не берётся из статичного allowlist (Security ASVS V4): человек
мог быть снят с роли между вызовами, а любой ТЕКУЩИЙ админ должен проходить
проверку, не только владелец бота.

Вызывается из ChatAdminFilter (bot/filters/chat_admin.py) и напрямую из
bot/handlers/backfill.py.
"""

from __future__ import annotations

from aiogram import Bot
from aiogram.utils.chat_member import ADMINS


async def is_chat_admin(bot: Bot, chat_id: int, user_id: int) -> bool:
    """Live-проверка: является ли user_id администратором/владельцем chat_id.

    Всегда обращается к Telegram через bot.get_chat_member — не кэшируется,
    не доверяет клиентскому флагу "я админ" (D-02, Security ASVS V4).
    """
    member = await bot.get_chat_member(chat_id, user_id)
    return isinstance(member, ADMINS)
