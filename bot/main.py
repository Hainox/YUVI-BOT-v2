from __future__ import annotations

import asyncio
import importlib
import logging
import pkgutil

from aiogram import Bot
from aiogram import Dispatcher
from aiogram import Router

import bot.handlers as handlers_package
from bot.config import settings
from bot.middleware.collector import CollectorMiddleware
from bot.middleware.db_session import DbSessionMiddleware
from bot.services import profanity_service
from bot.services.commands_service import setup_bot_commands
from bot.services.pinned_menu_service import ensure_pinned_menu
from bot.services.scheduler import get_scheduler
from bot.services.scheduler import setup_jobs
from common.db.session import SessionLocal

logger = logging.getLogger(__name__)


def _discover_routers() -> list[Router]:
    """Импортирует все модули bot.handlers и собирает их атрибуты `router`.

    Детерминированный порядок (sorted по имени модуля), чтобы регистрация
    была воспроизводима между запусками. Каждый новый bot/handlers/*.py с
    `router = Router()` подключается автоматически, без правки этого файла.
    """
    routers: list[Router] = []
    module_infos = sorted(
        pkgutil.iter_modules(handlers_package.__path__), key=lambda m: m.name
    )
    for module_info in module_infos:
        module = importlib.import_module(f"{handlers_package.__name__}.{module_info.name}")
        router = getattr(module, "router", None)
        if isinstance(router, Router):
            routers.append(router)
    return routers


async def run() -> None:
    logging.basicConfig(level=settings.log_level)

    bot = Bot(token=settings.bot_token)
    dp = Dispatcher()

    await setup_bot_commands(bot)

    # Короткая самодостаточная сессия только для авто-закрепа (D-01/D-02) —
    # форма scheduler.py::register (сам открывает SessionLocal), в run() нет
    # долгоживущей AsyncSession.
    # Best-effort: авто-закреп — это одноразовая UX-плюшка (входное сообщение),
    # а не критичная для работы бота операция, поэтому любая непредвиденная
    # ошибка здесь логируется и не должна блокировать старт поллинга (CR-01).
    try:
        async with SessionLocal() as pinned_menu_session:
            await ensure_pinned_menu(bot, pinned_menu_session, settings.chat_id)
    except Exception:
        logger.exception("run(): ensure_pinned_menu failed, continuing startup without pin")

    # Middleware регистрируется до подключения роутеров ниже: DbSession — для
    # каждого апдейта, Collector — только для message-апдейтов (DATA-01, команды
    # не теряются).
    dp.update.outer_middleware(DbSessionMiddleware())
    dp.message.outer_middleware(CollectorMiddleware())

    for router in _discover_routers():
        dp.include_router(router)

    scheduler = get_scheduler()
    setup_jobs(bot)
    scheduler.start()

    # Прогрев MorphAnalyzer (~50 МБ, 2-5 сек) ДО старта поллинга, чтобы холодный
    # cold-load не блокировал обработку первого сообщения чата (Pitfall 6).
    profanity_service.init()

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
