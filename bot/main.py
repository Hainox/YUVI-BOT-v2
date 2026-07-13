from __future__ import annotations

import asyncio
import logging

from aiogram import Bot
from aiogram import Dispatcher

from bot.config import settings
from bot.handlers.basic import router as basic_router


async def run() -> None:
    logging.basicConfig(level=settings.log_level)

    bot = Bot(token=settings.bot_token)
    dp = Dispatcher()
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

