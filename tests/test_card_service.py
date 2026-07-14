"""Тесты bot/services/card_service.py (AI-03/D-04).

get_user_nlp_averages — интеграционный тест против живого Postgres (реальные
SELECT AVG(...)/COUNT(...) FILTER над messages). test_card_reuses_stats_service
мокает ai_client.stream (без реального похода к OpenCode Go — которого сейчас
нет по биллингу) и spy-обёрткой доказывает, что build_card зовёт
stats_service.get_user_stats/get_streak/get_top_words НАПРЯМУЮ — без
дублирующего SQL по daily_stats/word_frequency внутри card_service.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import date
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from bot.services import card_service
from bot.services import stats_service
from common.models.daily_stat import DailyStat
from common.models.message import Message
from common.models.user import User
from common.models.word_frequency import WordFrequency

MSK = ZoneInfo("Europe/Moscow")


async def _ensure_user(session, user_id: int, first_name: str = "Тест") -> None:
    session.add(User(id=user_id, first_name=first_name))
    await session.flush()


async def _seed_message(
    session,
    chat_id: int,
    user_id: int,
    telegram_message_id: int,
    *,
    text: str | None = "сообщение",
    sentiment_score: float | None = None,
    toxicity_score: float | None = None,
    classified: bool = False,
) -> None:
    message = Message(
        chat_id=chat_id,
        user_id=user_id,
        telegram_message_id=telegram_message_id,
        text=text,
        sentiment_score=sentiment_score,
        toxicity_score=toxicity_score,
        nlp_processed_at=datetime.now(MSK).replace(tzinfo=None) if classified else None,
    )
    session.add(message)
    await session.flush()


async def _fake_stream(messages: list[dict], model: str, max_tokens: int) -> AsyncIterator[str]:
    """Канонический async-генератор вместо реального похода к OpenCode Go
    (see test suite note: у аккаунта сейчас нет валидного биллинга — реальные
    LLM-вызовы недоступны, тесты этого плана мокают ai_client.stream)."""
    for part in ["Душа компании, ", "шутит в любое время дня и ночи."]:
        yield part


# --- get_user_nlp_averages --------------------------------------------------


@pytest.mark.asyncio
async def test_get_user_nlp_averages_returns_no_data_marker_when_nothing_classified(session):
    chat_id = -100931000001
    user_id = 700200001
    await _ensure_user(session, user_id)
    await _seed_message(session, chat_id, user_id, 1)  # не классифицировано

    nlp = await card_service.get_user_nlp_averages(session, chat_id, user_id)

    assert nlp == {"classified_count": 0, "avg_sentiment": None, "avg_toxicity": None}


@pytest.mark.asyncio
async def test_get_user_nlp_averages_computes_per_user_averages(session):
    chat_id = -100931000002
    user_id = 700200002
    other_user_id = 700200003
    await _ensure_user(session, user_id, "Целевой")
    await _ensure_user(session, other_user_id, "Другой")

    await _seed_message(
        session, chat_id, user_id, 1,
        sentiment_score=1.0, toxicity_score=0.2, classified=True,
    )
    await _seed_message(
        session, chat_id, user_id, 2,
        sentiment_score=0.6, toxicity_score=0.4, classified=True,
    )
    # Сообщение ДРУГОГО участника того же чата — не должно попасть в средние.
    await _seed_message(
        session, chat_id, other_user_id, 3,
        sentiment_score=0.0, toxicity_score=0.9, classified=True,
    )

    nlp = await card_service.get_user_nlp_averages(session, chat_id, user_id)

    assert nlp["classified_count"] == 2
    assert nlp["avg_sentiment"] == pytest.approx(0.8)
    assert nlp["avg_toxicity"] == pytest.approx(0.3)


@pytest.mark.asyncio
async def test_get_user_nlp_averages_returns_zero_for_unknown_chat(session):
    nlp = await card_service.get_user_nlp_averages(session, chat_id=-1, user_id=-1)

    assert nlp == {"classified_count": 0, "avg_sentiment": None, "avg_toxicity": None}


# --- build_portrait ----------------------------------------------------------


@pytest.mark.asyncio
async def test_build_portrait_returns_placeholder_when_no_messages(session, monkeypatch):
    """Нет сообщений участника -> заглушка, ai_client.stream не вызывается вовсе."""

    def _boom(*args, **kwargs):
        raise AssertionError("build_portrait не должен звать ai_client.stream без сообщений")

    monkeypatch.setattr(card_service.ai_client, "stream", _boom)

    chat_id = -100931000004
    user_id = 700200004
    await _ensure_user(session, user_id)

    portrait = await card_service.build_portrait(session, chat_id, user_id, "Тест")

    assert portrait == card_service.NO_DATA_PORTRAIT


@pytest.mark.asyncio
async def test_build_portrait_returns_llm_text_when_messages_exist(session, monkeypatch):
    monkeypatch.setattr(card_service.ai_client, "stream", _fake_stream)

    chat_id = -100931000005
    user_id = 700200005
    await _ensure_user(session, user_id, "Портретный")
    await _seed_message(session, chat_id, user_id, 1, text="привет всем")

    portrait = await card_service.build_portrait(session, chat_id, user_id, "Портретный")

    assert portrait == "Душа компании, шутит в любое время дня и ночи."


# --- build_card: переиспользование stats_service (D-04) ---------------------


@pytest.mark.asyncio
async def test_card_reuses_stats_service(session, monkeypatch):
    chat_id = -100931000006
    user_id = 700200006
    today = datetime.now(MSK).date()

    await _ensure_user(session, user_id, "Карточкин")
    session.add(DailyStat(chat_id=chat_id, user_id=user_id, stat_date=today, message_count=5))
    session.add(WordFrequency(chat_id=chat_id, user_id=user_id, word="привет", count=3))
    await session.flush()
    await _seed_message(
        session, chat_id, user_id, 1,
        text="привет чат", sentiment_score=0.8, toxicity_score=0.1, classified=True,
    )

    monkeypatch.setattr(card_service.ai_client, "stream", _fake_stream)

    calls = {"get_user_stats": 0, "get_streak": 0, "get_top_words": 0}
    orig_get_user_stats = stats_service.get_user_stats
    orig_get_streak = stats_service.get_streak
    orig_get_top_words = stats_service.get_top_words

    async def _tracking_get_user_stats(*args, **kwargs):
        calls["get_user_stats"] += 1
        return await orig_get_user_stats(*args, **kwargs)

    async def _tracking_get_streak(*args, **kwargs):
        calls["get_streak"] += 1
        return await orig_get_streak(*args, **kwargs)

    async def _tracking_get_top_words(*args, **kwargs):
        calls["get_top_words"] += 1
        return await orig_get_top_words(*args, **kwargs)

    monkeypatch.setattr(stats_service, "get_user_stats", _tracking_get_user_stats)
    monkeypatch.setattr(stats_service, "get_streak", _tracking_get_streak)
    monkeypatch.setattr(stats_service, "get_top_words", _tracking_get_top_words)

    card = await card_service.build_card(session, chat_id, user_id, "Карточкин")

    # D-04: stats-блок построен через stats_service, не собственным SQL.
    assert calls == {"get_user_stats": 1, "get_streak": 1, "get_top_words": 1}

    # Структура из трёх блоков.
    assert set(card.keys()) == {"portrait", "stats", "nlp"}

    assert card["portrait"] == "Душа компании, шутит в любое время дня и ночи."

    assert card["stats"]["total_messages"] == 5
    assert card["stats"]["streak"] == 1
    assert card["stats"]["top_words"] == [{"word": "привет", "count": 3}]

    assert card["nlp"]["classified_count"] == 1
    assert card["nlp"]["avg_sentiment"] == pytest.approx(0.8)
    assert card["nlp"]["avg_toxicity"] == pytest.approx(0.1)


@pytest.mark.asyncio
async def test_card_service_does_not_duplicate_daily_stat_sql(session):
    """card_service не должен импортировать DailyStat/WordFrequency напрямую —
    stats-блок обязан идти через stats_service (D-04), а не собственный ORM-запрос."""
    import inspect

    source = inspect.getsource(card_service)
    assert "DailyStat" not in source
    assert "WordFrequency" not in source
