"""Singleton APScheduler для фоновых задач бота (единственный на весь репозиторий).

get_scheduler() лениво создаёт и возвращает один и тот же AsyncIOScheduler
(таймзона Europe/Moscow — все игровые/дайджест-сбросы идут по МСК).

setup_jobs(bot) — единая точка расширения: регистрирует фоновую NLP-
классификацию (nlp_classifier, interval 30с, NLP-02) и эмбеддинг-воркер
(embed_worker, interval 45с) через их register(scheduler, bot). Импорты
ленивые (внутри функции), чтобы модули, ещё не существующие на момент
плана 01 (пустой setup_jobs), не ломали import bot.main до их появления.
Автодайджест (D-01/D-02/D-03) добавит план 09 сюда же.
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


def setup_jobs(bot: Bot) -> None:
    """Точка расширения для фоновых job'ов.

    Регистрирует NLP-классификацию (NLP-02) и эмбеддинг-воркер. Ленивый
    импорт — эти модули появились только в плане 05; план 09 (автодайджест)
    дополнит эту функцию ещё одним register(...) вызовом.
    """
    from bot.services import embed_worker
    from bot.services import nlp_classifier

    scheduler = get_scheduler()
    nlp_classifier.register(scheduler, bot)
    embed_worker.register(scheduler, bot)
