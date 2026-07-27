"""Тесты bot/services/card_service.py (AI-03/D-04, формат карточки 2026-07-27).

test_card_reuses_stats_service мокает ai_client.stream (без реального похода к
OpenCode Go — которого сейчас нет по биллингу) и spy-обёрткой доказывает, что
build_card зовёт stats_service.get_user_stats НАПРЯМУЮ — без дублирующего SQL
по daily_stats внутри card_service.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from bot.services import card_service
from bot.services import stats_service
from common.models.daily_stat import DailyStat
from common.models.message import Message
from common.models.user import User

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


# --- get_user_avg_message_length ---------------------------------------------


@pytest.mark.asyncio
async def test_get_user_avg_message_length_computes_per_user_average(session):
    chat_id = -100931000001
    user_id = 700200001
    other_user_id = 700200002
    await _ensure_user(session, user_id, "Целевой")
    await _ensure_user(session, other_user_id, "Другой")

    await _seed_message(session, chat_id, user_id, 1, text="1234")  # 4 симв.
    await _seed_message(session, chat_id, user_id, 2, text="123456")  # 6 симв.
    # Сообщение ДРУГОГО участника того же чата — не должно попасть в среднее.
    await _seed_message(session, chat_id, other_user_id, 3, text="1")

    avg_length = await card_service.get_user_avg_message_length(session, chat_id, user_id)

    assert avg_length == pytest.approx(5.0)


@pytest.mark.asyncio
async def test_get_user_avg_message_length_zero_when_no_messages(session):
    avg_length = await card_service.get_user_avg_message_length(session, chat_id=-1, user_id=-1)

    assert avg_length == 0.0


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


# --- build_profile_sections ---------------------------------------------------


@pytest.mark.asyncio
async def test_build_profile_sections_returns_placeholder_when_no_messages(session, monkeypatch):
    def _boom(*args, **kwargs):
        raise AssertionError("build_profile_sections не должен звать ai_client.stream без сообщений")

    monkeypatch.setattr(card_service.ai_client, "stream", _boom)

    chat_id = -100931000006
    user_id = 700200006
    await _ensure_user(session, user_id)
    stats = {
        "total_messages": 0, "active_days": 0,
        "first_active_date": None, "last_active_date": None,
    }

    profile = await card_service.build_profile_sections(
        session, chat_id, user_id, "Тест", stats, 0.0, [], []
    )

    assert profile == card_service.NO_DATA_PROFILE


@pytest.mark.asyncio
async def test_build_profile_sections_embeds_real_numbers_in_prompt(session, monkeypatch):
    """ЦИФРЫ-блок должен интерпретировать РЕАЛЬНЫЕ числа карточки — доказываем,
    что build_profile_sections передаёт их в промпт, а не оставляет LLM
    гадать/выдумывать."""
    captured_messages: list[dict] = []

    async def capturing_stream(messages, model, max_tokens):
        captured_messages.extend(messages)
        for chunk in ["ОБРАЗ\n", "текст"]:
            yield chunk

    monkeypatch.setattr(card_service.ai_client, "stream", capturing_stream)

    chat_id = -100931000007
    user_id = 700200007
    await _ensure_user(session, user_id, "Числовой")
    await _seed_message(session, chat_id, user_id, 1, text="привет")

    stats = {
        "total_messages": 42, "active_days": 7,
        "first_active_date": "2026-01-01", "last_active_date": "2026-07-01",
    }
    reactions_received = [{"emoji": "❤️", "count": 3}]
    reactions_given = [{"emoji": "🤣", "count": 1}]

    profile = await card_service.build_profile_sections(
        session, chat_id, user_id, "Числовой", stats, 12.5, reactions_received, reactions_given
    )

    assert profile == "ОБРАЗ\nтекст"
    system_prompt = captured_messages[0]["content"]
    assert "42" in system_prompt
    assert "7" in system_prompt
    assert "12" in system_prompt  # avg_length отформатирован :.0f
    assert "❤️×3" in system_prompt
    assert "🤣×1" in system_prompt


# --- build_card: переиспользование stats_service (D-04) ---------------------


@pytest.mark.asyncio
async def test_card_reuses_stats_service(session, monkeypatch):
    chat_id = -100931000008
    user_id = 700200008
    today = datetime.now(MSK).date()

    await _ensure_user(session, user_id, "Карточкин")
    session.add(DailyStat(chat_id=chat_id, user_id=user_id, stat_date=today, message_count=5))
    await session.flush()
    await _seed_message(session, chat_id, user_id, 1, text="привет чат чат чат")  # 18 симв.

    monkeypatch.setattr(card_service.ai_client, "stream", _fake_stream)

    calls = {"get_user_stats": 0}
    orig_get_user_stats = stats_service.get_user_stats

    async def _tracking_get_user_stats(*args, **kwargs):
        calls["get_user_stats"] += 1
        return await orig_get_user_stats(*args, **kwargs)

    monkeypatch.setattr(stats_service, "get_user_stats", _tracking_get_user_stats)

    card = await card_service.build_card(session, chat_id, user_id, "Карточкин")

    # D-04: stats-блок построен через stats_service, не собственным SQL.
    assert calls == {"get_user_stats": 1}

    assert set(card.keys()) == {
        "stats", "avg_length", "reactions_received", "reactions_given", "profile",
    }

    assert card["stats"]["total_messages"] == 5
    assert card["avg_length"] == pytest.approx(18.0)
    assert card["reactions_received"] == []
    assert card["reactions_given"] == []
    assert card["profile"] == "Душа компании, шутит в любое время дня и ночи."


@pytest.mark.asyncio
async def test_card_service_does_not_duplicate_daily_stat_sql(session):
    """card_service не должен импортировать DailyStat/WordFrequency напрямую —
    stats-блок обязан идти через stats_service (D-04), а не собственный ORM-запрос."""
    import inspect

    source = inspect.getsource(card_service)
    assert "DailyStat" not in source
    assert "WordFrequency" not in source
