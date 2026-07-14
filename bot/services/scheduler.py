"""Singleton APScheduler для фоновых задач бота (единственный на весь репозиторий).

get_scheduler() лениво создаёт и возвращает один и тот же AsyncIOScheduler
(таймзона Europe/Moscow — все игровые/дайджест-сбросы идут по МСК).

setup_jobs(bot) — единая точка расширения: регистрирует фоновую NLP-
классификацию (nlp_classifier, interval 30с, NLP-02), эмбеддинг-воркер
(embed_worker, interval 45с) через их register(scheduler, bot),
автодайджест (digest_daily, cron hour=22 Europe/Moscow, D-01) через
digest_service.run_daily_digest, и auto-close просроченных рынков ставок
(markets_auto_close, interval 5м) через markets_service.register_auto_close
(план 03-05). Импорты ленивые (внутри функции), чтобы модули, ещё не
существующие на момент плана 01 (пустой setup_jobs), не ломали import
bot.main до их появления.
"""

from __future__ import annotations

from zoneinfo import ZoneInfo

from aiogram import Bot
from apscheduler.schedulers.asyncio import AsyncIOScheduler

MSK = ZoneInfo("Europe/Moscow")

_scheduler: AsyncIOScheduler | None = None


def get_scheduler() -> AsyncIOScheduler:
    """Возвращает singleton AsyncIOScheduler (создаёт при первом вызове)."""
    global _scheduler
    if _scheduler is None:
        _scheduler = AsyncIOScheduler(timezone=MSK)
    return _scheduler


_DIGEST_JOB_ID = "digest_daily"


def setup_jobs(bot: Bot) -> None:
    """Точка расширения для фоновых job'ов.

    Регистрирует NLP-классификацию (NLP-02), эмбеддинг-воркер (план 05),
    автодайджест (план 09, D-01: раз в день, 22:00 МСК) и auto-close
    просроченных рынков ставок (план 03-05, markets_auto_close, interval
    5м). Ленивые импорты — эти модули появились в более поздних планах,
    чем изначальный (пустой) setup_jobs плана 01.
    """
    from bot.services import digest_service
    from bot.services import embed_worker
    from bot.services import markets_service
    from bot.services import nlp_classifier

    scheduler = get_scheduler()
    nlp_classifier.register(scheduler, bot)
    embed_worker.register(scheduler, bot)
    markets_service.register_auto_close(scheduler)

    async def _digest_job() -> None:
        await digest_service.run_daily_digest(bot)

    # D-01/D-02: раз в день в 22:00 МСК. coalesce+max_instances=1 — если бот
    # был офлайн и APScheduler видит несколько пропущенных срабатываний,
    # выполняется только одно, не шлём дайджест N раз (T-02-22). misfire_grace_time
    # даёт запуск, даже если бот поднялся с опозданием в пределах часа.
    scheduler.add_job(
        _digest_job,
        "cron",
        hour=22,
        minute=0,
        timezone=MSK,
        coalesce=True,
        max_instances=1,
        misfire_grace_time=3600,
        id=_DIGEST_JOB_ID,
        replace_existing=True,
    )
