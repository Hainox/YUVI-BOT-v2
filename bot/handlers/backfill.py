"""Роутер команды /backfill (DATA-04, D-01/D-02).

Тонкий хендлер: гейт admin_service.is_chat_admin (D-02, live-проверка) ->
asyncio.create_task(backfill_service.run_backfill) -> сразу отвечает в группе.
Backfill всей истории (D-01) идёт в фоне, не блокируя хендлер — in-process,
без subprocess: единственный чат, изоляция процессов не нужна, а subprocess
потребовал бы прокидывать TG_API_ID/TG_API_HASH через границу процесса без
пользы.
"""

from __future__ import annotations

import asyncio

from aiogram import Bot
from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from bot.services import admin_service
from bot.services import backfill_service

router = Router()


@router.message(Command("backfill"))
async def cmd_backfill(message: Message, bot: Bot) -> None:
    if message.from_user is None or not await admin_service.is_chat_admin(
        bot, message.chat.id, message.from_user.id
    ):
        await message.reply("Только администратор чата может запускать backfill.")
        return

    asyncio.create_task(backfill_service.run_backfill(message.chat.id))
    await message.answer("Backfill запущен, историю подтянем в фоне.")
