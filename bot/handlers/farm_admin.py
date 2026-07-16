"""/farmwipe (FARM-03): административный сброс фермы участника. Тонкий
хендлер: парсит вход, резолвит цель, зовёт `clicker_service.wipe_farm` —
вся экономическая логика сброса в сервисе (форма `bot/handlers/duel.py`
докстринг: "тонкий хендлер... вся денежная/статусная логика в сервисе").

D-03 (форма `duel.py::unmute_command`/`backfill.py`): ручной live-гейт
`admin_service.is_chat_admin` с явным отказом не-админу (не молчаливый
`ChatAdminFilter`) — любой ТЕКУЩИЙ админ чата может сбросить чужую ферму.

Резолв цели — reply > text_mention entity > @username/id-аргумент, из общего
`bot/services/target_resolution_service.py` (WR-04, 04.2-REVIEW: раньше был
продублирован byte-for-byte в `economy.py`/`duel.py`/`farm_admin.py`, теперь
живёт в одном месте).
"""

from __future__ import annotations

import html
import logging

from aiogram import Bot
from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.services import admin_service
from bot.services import clicker_service
from bot.services.target_resolution_service import resolve_target

logger = logging.getLogger(__name__)

router = Router()


def _parse_target_arg(message: Message) -> str | None:
    """Парсит `/farmwipe <@username|id>` (без reply) — единственный текстовый
    токен (форма `duel.py::_parse_single_target_arg`)."""
    if message.text is None:
        return None
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        return None
    token = parts[1].strip().split()[0] if parts[1].strip() else ""
    return token or None


@router.message(Command("farmwipe"))
async def farmwipe_command(message: Message, session: AsyncSession, bot: Bot) -> None:
    """D-03: только текущий (live-проверка) админ чата, явный отказ
    не-админу."""
    if message.from_user is None:
        return
    if not await admin_service.is_chat_admin(bot, message.chat.id, message.from_user.id):
        await message.reply("Только администратор чата может сбросить ферму.")
        return

    target_arg = _parse_target_arg(message)
    target = await resolve_target(message, session, target_arg)
    if target is None:
        await message.answer(
            "Использование: /farmwipe (ответом на сообщение цели) или /farmwipe @username"
        )
        return

    target_id, target_name = target
    await clicker_service.wipe_farm(session, message.chat.id, target_id)
    await message.answer(
        f"Ферма {html.escape(target_name)} сброшена: CP, уровни тапа/автокликера обнулены.",
        parse_mode="HTML",
    )
