"""Read-only агрегаты настроения/токсичности чата (NLP-03).

ЧИСТЫЙ SQL над заранее посчитанными колонками messages.sentiment_score /
sentiment_label / toxicity_score (наполняются фоновым job'ом плана 02-05,
bot/services/nlp_classifier.py). Этот модуль НИКОГДА не импортирует клиенты
внешних AI/NLP-сервисов и не делает никаких LLM/HTTP-вызовов — только
SELECT ... AVG(...)/COUNT(...) FILTER (WHERE ...) над таблицей messages
(RESEARCH.md Anti-Patterns: /mood и /toxic не зовут LLM/NLP синхронно).
"""

from __future__ import annotations

from datetime import date
from datetime import datetime
from datetime import timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from common.models.message import Message

MSK = ZoneInfo("Europe/Moscow")

TOXIC_THRESHOLD = 0.5


def _today_msk() -> date:
    return datetime.now(MSK).date()


def _since_date(days: int | None) -> date | None:
    """D-06: days=None -> всё время (None). Иначе — дата начала периода
    (включая сегодня, по дате в Europe/Moscow)."""
    if days is None:
        return None
    return _today_msk() - timedelta(days=days)


async def get_chat_mood(session: AsyncSession, chat_id: int, days: int | None = None) -> dict:
    """Агрегированная тональность чата (для /mood).

    D-06: days=None — за всё время; иначе — за последние N дней.
    Возвращает {classified_count, avg_sentiment, label_shares}. Если по чату
    нет классифицированных сообщений за период — classified_count=0,
    avg_sentiment/label_shares=None (без деления на ноль).
    """
    classified_count_col = func.count().filter(Message.sentiment_score.isnot(None))
    positive_col = func.count().filter(Message.sentiment_label == "positive")
    neutral_col = func.count().filter(Message.sentiment_label == "neutral")
    negative_col = func.count().filter(Message.sentiment_label == "negative")

    stmt = select(
        func.avg(Message.sentiment_score),
        classified_count_col,
        positive_col,
        neutral_col,
        negative_col,
    ).where(Message.chat_id == chat_id)
    since_date = _since_date(days)
    if since_date is not None:
        stmt = stmt.where(Message.created_at >= since_date)

    result = await session.execute(stmt)
    avg_sentiment, classified_count, positive, neutral, negative = result.one()
    classified_count = int(classified_count)

    if classified_count == 0:
        return {"classified_count": 0, "avg_sentiment": None, "label_shares": None}

    return {
        "classified_count": classified_count,
        "avg_sentiment": float(avg_sentiment),
        "label_shares": {
            "positive": int(positive) / classified_count,
            "neutral": int(neutral) / classified_count,
            "negative": int(negative) / classified_count,
        },
    }


async def get_chat_toxicity(session: AsyncSession, chat_id: int, days: int | None = None) -> dict:
    """Агрегированная токсичность чата (для /toxic).

    D-06: days=None — за всё время; иначе — за последние N дней.
    Возвращает {classified_count, avg_toxicity, toxic_share}. Если по чату
    нет классифицированных сообщений за период — classified_count=0,
    avg_toxicity/toxic_share=None (без деления на ноль).
    """
    classified_count_col = func.count().filter(Message.toxicity_score.isnot(None))
    toxic_col = func.count().filter(Message.toxicity_score > TOXIC_THRESHOLD)

    stmt = select(
        func.avg(Message.toxicity_score),
        classified_count_col,
        toxic_col,
    ).where(Message.chat_id == chat_id)
    since_date = _since_date(days)
    if since_date is not None:
        stmt = stmt.where(Message.created_at >= since_date)

    result = await session.execute(stmt)
    avg_toxicity, classified_count, toxic_count = result.one()
    classified_count = int(classified_count)

    if classified_count == 0:
        return {"classified_count": 0, "avg_toxicity": None, "toxic_share": None}

    return {
        "classified_count": classified_count,
        "avg_toxicity": float(avg_toxicity),
        "toxic_share": int(toxic_count) / classified_count,
    }
