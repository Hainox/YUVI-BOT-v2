"""Роутер команды /backfill (DATA-04, D-01/D-02).

Тонкий хендлер: гейт admin_service.is_chat_admin (D-02, live-проверка) ->
asyncio.create_task(_run_backfill_and_report) -> сразу отвечает в группе.
Backfill всей истории (D-01) идёт в фоне, не блокируя хендлер — in-process,
без subprocess: единственный чат, изоляция процессов не нужна, а subprocess
потребовал бы прокидывать TG_API_ID/TG_API_HASH через границу процесса без
пользы. Фоновая задача сама сообщает результат (успех/ошибку) обратно в
группу — иначе исключение (например, отсутствующие TG_API_ID/TG_API_HASH)
уходит только в логи контейнера и пользователь никогда не узнаёт, что
backfill не запустился.
"""

from __future__ import annotations

import asyncio
import logging

from aiogram import Bot
from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from bot.services import admin_service
from bot.services import backfill_service

router = Router()
logger = logging.getLogger(__name__)

# Client("yuvi_backfill_session", ...) в backfill_service всегда открывает
# ОДИН и тот же файл SQLite-сессии, независимо от chat_id. Два одновременных
# запуска (двойной /backfill подряд, или запуск в разных чатах в одно и то
# же время) бьются за один файл и падают с sqlite3.OperationalError:
# database is locked (см. run_backfill упал в логах). Лок сериализует
# фоновые прогоны в этом процессе; второй просто дожидается первого и
# честно репортит свой результат (backfill идемпотентен — повторный прогон
# безопасен, см. docs/backfill-setup.md §5).
_backfill_lock = asyncio.Lock()


async def _run_backfill_and_report(bot: Bot, chat_id: int) -> None:
    async with _backfill_lock:
        try:
            total_inserted = await backfill_service.run_backfill(chat_id)
        except Exception as exc:  # noqa: BLE001 - фоновая задача обязана сообщить любую ошибку в чат
            logger.exception("run_backfill упал для chat_id=%s", chat_id)
            await bot.send_message(chat_id, f"Backfill завершился с ошибкой: {exc}")
            return
        await bot.send_message(
            chat_id, f"Backfill завершён: добавлено новых сообщений — {total_inserted}."
        )


@router.message(Command("backfill"))
async def cmd_backfill(message: Message, bot: Bot) -> None:
    if message.from_user is None or not await admin_service.is_chat_admin(
        bot, message.chat.id, message.from_user.id
    ):
        await message.reply("Только администратор чата может запускать backfill.")
        return

    asyncio.create_task(_run_backfill_and_report(bot, message.chat.id))
    await message.answer("Backfill запущен, историю подтянем в фоне.")
