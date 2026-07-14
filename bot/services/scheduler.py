"""Singleton APScheduler для фоновых задач бота (единственный на весь репозиторий).

get_scheduler() лениво создаёт и возвращает один и тот же AsyncIOScheduler
(таймзона Europe/Moscow — все игровые/дайджест-сбросы идут по МСК).

setup_jobs(bot) — единая точка расширения: сюда планы 05 (автодайджест,
D-01/D-02/D-03) и 09 (фоновая NLP-классификация/эмбеддинги, NLP-02) добавляют
свои job'ы через scheduler.add_job(...). В Фазе 2 плане 01 функция намеренно
пустая — здесь НЕ импортируются ещё не существующие модули nlp_classifier/
digest_service.
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

    Пока пустая: планы 05 (автодайджест) и 09 (NLP-классификация/эмбеддинги)
    добавят сюда scheduler.add_job(...) вызовы.
    """
