"""Сборка карточки участника `/card` (AI-03, D-04) из трёх блоков:

1. AI-портрет — короткий юмористический текст по истории сообщений участника
   (ai_client.stream, промпт собирается здесь).
2. Статистика Фазы 1 — ПРЯМОЕ переиспользование stats_service.get_user_stats/
   get_streak/get_top_words, без дублирования SQL (D-04/Reusable Assets).
3. NLP-средние — per-user вариант mood_service.get_chat_mood/get_chat_toxicity
   (тот же принцип: чистый SQL над заранее посчитанными колонками
   messages.sentiment_score/toxicity_score/nlp_processed_at, никаких
   синхронных LLM/NLP-вызовов для этого блока).
"""

from __future__ import annotations

from sqlalchemy import func
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import settings
from bot.services import ai_client
from bot.services import settings_service
from bot.services import stats_service
from bot.services.summary_service import CHARS_PER_TOKEN
from bot.services.summary_service import build_context
from common.models.message import Message
from common.models.user import User

PORTRAIT_MESSAGE_LIMIT = 50
TOP_WORDS_LIMIT = 5
NO_DATA_PORTRAIT = "Пока маловато сообщений от этого участника для портрета — попробуйте позже."


async def get_user_nlp_averages(session: AsyncSession, chat_id: int, user_id: int) -> dict:
    """Средние sentiment/toxicity КОНКРЕТНОГО участника из уже посчитанных
    колонок messages (NLP-01/02) — per-user вариант mood_service. Классифицированным
    считается nlp_processed_at IS NOT NULL (nlp_classifier.py пишет обе метрики
    и nlp_processed_at одной транзакцией). Без данных -> None-поля, без деления на ноль.
    """
    classified_count_col = func.count().filter(Message.nlp_processed_at.isnot(None))
    stmt = select(
        func.avg(Message.sentiment_score),
        func.avg(Message.toxicity_score),
        classified_count_col,
    ).where(Message.chat_id == chat_id, Message.user_id == user_id)

    result = await session.execute(stmt)
    avg_sentiment, avg_toxicity, classified_count = result.one()
    classified_count = int(classified_count)

    if classified_count == 0:
        return {"classified_count": 0, "avg_sentiment": None, "avg_toxicity": None}

    return {
        "classified_count": classified_count,
        "avg_sentiment": float(avg_sentiment) if avg_sentiment is not None else None,
        "avg_toxicity": float(avg_toxicity) if avg_toxicity is not None else None,
    }


async def fetch_user_recent_texts(
    session: AsyncSession, chat_id: int, user_id: int, n: int
) -> list[dict]:
    """Последние N текстовых сообщений КОНКРЕТНОГО участника, в хронологическом
    порядке (от старых к новым — build_context ожидает именно такой порядок).

    Аналог summary_service.fetch_recent_texts + фильтр по user_id (per-card
    вариант; здесь намеренно НЕ переиспользуется fetch_recent_texts напрямую,
    т.к. у неё нет параметра user_id — добавлять его туда расширило бы
    контракт функции, используемой /summary для всего чата).

    Публичная (WR-04, 05-REVIEW.md) — вызывается не только build_portrait
    отсюда, но и напрямую из twin_service.build_twin_reply; leading-
    underscore имя неверно сигнализировало бы "internal only", хотя у
    функции есть реальный внешний вызывающий.
    """
    stmt = (
        select(Message.text, User.first_name)
        .outerjoin(User, User.id == Message.user_id)
        .where(Message.chat_id == chat_id, Message.user_id == user_id, Message.text.is_not(None))
        .order_by(Message.created_at.desc(), Message.id.desc())
        .limit(n)
    )
    result = await session.execute(stmt)
    rows = list(reversed(result.all()))
    return [{"author": row.first_name or "Аноним", "text": row.text} for row in rows]


async def build_portrait(
    session: AsyncSession, chat_id: int, user_id: int, display_name: str
) -> str:
    """Короткий юмористический портрет участника по истории его сообщений
    (AI-03/D-04, блок 1). При отсутствии сообщений — заглушка, не падение.

    Стриминг в сообщение не нужен (портрет короткий, часть большого ответа
    /card) — собираем полный текст из ai_client.stream и возвращаем строкой.
    """
    rows = await fetch_user_recent_texts(session, chat_id, user_id, PORTRAIT_MESSAGE_LIMIT)
    if not rows:
        return NO_DATA_PORTRAIT

    char_budget = settings.ai_max_input_tokens * CHARS_PER_TOKEN
    context = build_context(rows, char_budget)

    system_prompt = await settings_service.get_active_prompt(session, chat_id)
    system_prompt += (
        f"\n\nНа основе сообщений участника {display_name} напиши короткий "
        "юмористический портрет — какой этот человек в чате, 2-4 предложения, "
        "по-доброму, без оскорблений."
    )
    model = await settings_service.get_active_model(session, chat_id)
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": context},
    ]

    parts = [
        delta
        async for delta in ai_client.stream(
            messages, model=model, max_tokens=settings.ai_max_output_tokens
        )
    ]
    return "".join(parts)


async def build_card(session: AsyncSession, chat_id: int, user_id: int, display_name: str) -> dict:
    """Собирает три блока карточки участника (D-04): portrait, stats, nlp.

    stats-блок — ПРЯМОЙ вызов stats_service.get_user_stats/get_streak/
    get_top_words (без дублирующего SQL по daily_stats/word_frequency здесь).
    """
    portrait = await build_portrait(session, chat_id, user_id, display_name)

    user_stats = await stats_service.get_user_stats(session, chat_id, user_id)
    streak = await stats_service.get_streak(session, chat_id, user_id)
    top_words = await stats_service.get_top_words(session, chat_id, limit=TOP_WORDS_LIMIT)

    stats = {**user_stats, "streak": streak, "top_words": top_words}
    nlp = await get_user_nlp_averages(session, chat_id, user_id)

    return {"portrait": portrait, "stats": stats, "nlp": nlp}
