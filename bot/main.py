from __future__ import annotations

import asyncio
import logging

from aiogram import Bot
from aiogram import Dispatcher

from bot.config import settings
from bot.handlers.backfill import router as backfill_router
from bot.handlers.basic import router as basic_router
from bot.handlers.edits import router as edits_router
from bot.handlers.reactions import router as reactions_router
from bot.handlers.stats import router as stats_router
from bot.middleware.collector import CollectorMiddleware
from bot.middleware.db_session import DbSessionMiddleware


async def run() -> None:
    logging.basicConfig(level=settings.log_level)

    bot = Bot(token=settings.bot_token)
    dp = Dispatcher()

    # Middleware регистрируется до подключения роутеров ниже: DbSession — для
    # каждого апдейта, Collector — только для message-апдейтов (DATA-01, команды
    # не теряются).
    dp.update.outer_middleware(DbSessionMiddleware())
    dp.message.outer_middleware(CollectorMiddleware())

    dp.include_router(stats_router)
    dp.include_router(reactions_router)
    dp.include_router(edits_router)
    dp.include_router(backfill_router)
    dp.include_router(basic_router)

    await dp.start_polling(
        bot,
        allowed_updates=[
            "message",
            "message_reaction",
            "edited_message",
            "chat_member",
            "my_chat_member",
        ],
    )


if __name__ == "__main__":
    asyncio.run(run())
