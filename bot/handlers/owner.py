"""/grant (запрошено 2026-07-24): ручное начисление ювиков разработчиком
владельцем бота при спорных ситуациях (жалоба на баг казино/гачи/фермы,
компенсация и т.п.) — НЕ доступно обычным админам чата, только
`settings.owner_id` (форма `farm_admin.py`: тонкий хендлер, живая проверка
права с явным отказом, вся денежная логика — в `economy_service`).

Осознанно только выдача (не списание) — на отбор ювиков у пользователя
существующих команд/прав достаточно, а спор почти всегда решается в пользу
компенсации, а не штрафа.
"""

from __future__ import annotations

import html
import logging

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import settings
from bot.services import economy_service
from bot.services.target_resolution_service import resolve_by_username_or_id

logger = logging.getLogger(__name__)

router = Router()


def _parse_args(message: Message) -> tuple[str, int] | None:
    """Парсит `/grant <user_id|@username> <сумма>` — ровно два токена, сумма
    положительное целое."""
    if message.text is None:
        return None
    parts = message.text.split()
    if len(parts) != 3:
        return None
    target_arg, amount_raw = parts[1], parts[2]
    if not amount_raw.lstrip("-").isdigit():
        return None
    amount = int(amount_raw)
    if amount <= 0:
        return None
    return target_arg, amount


@router.message(Command("grant"))
async def grant_command(message: Message, session: AsyncSession) -> None:
    if message.from_user is None:
        return
    if message.from_user.id != settings.owner_id:
        await message.reply("Эта команда доступна только владельцу бота.")
        return

    parsed = _parse_args(message)
    if parsed is None:
        await message.answer("Использование: /grant <user_id|@username> <сумма>")
        return
    target_arg, amount = parsed

    target = await resolve_by_username_or_id(session, target_arg)
    if target is None:
        await message.answer(f"Пользователь {html.escape(target_arg)} не найден.")
        return
    target_id, target_name = target

    ref_id = f"owner_grant:{message.chat.id}:{message.message_id}"
    credited = await economy_service.credit(
        session, message.chat.id, target_id, amount, kind="owner_grant", ref_id=ref_id
    )
    await session.commit()
    if not credited:
        await message.answer("Это начисление уже было применено ранее.")
        return

    balance = await economy_service.get_balance(session, message.chat.id, target_id)
    await message.answer(
        f"Начислено {amount}¥ пользователю {html.escape(target_name)}. Баланс: {balance}¥.",
        parse_mode="HTML",
    )
    logger.info(
        "grant_command: owner=%s target=%s amount=%s chat=%s",
        message.from_user.id,
        target_id,
        amount,
        message.chat.id,
    )
