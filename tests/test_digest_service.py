"""Интеграционные тесты digest_service против живого Postgres (AI-02).

Доказывает D-03/D-12: build_digest пропускает дни с активностью ниже
settings.digest_min_messages, НЕ вызывая summary_service (никаких платных
LLM-обращений на скудный/пустой день, T-02-21). А при достаточной активности
собирает три блока (D-02): AI-пересказ, топ участников, настроение/
токсичность. summary_service.stream_summary и mood_service замоканы (по
инструкции плана) — этот файл не проверяет сам LLM/NLP-пайплайн, только
дисциплину порога и сборку дайджеста.
"""

from __future__ import annotations

from datetime import date
from datetime import datetime
from datetime import timedelta
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest

from bot.config import settings
from bot.services import digest_service
from common.models.daily_stat import DailyStat
from common.models.user import User

MSK = ZoneInfo("Europe/Moscow")


async def _ensure_user(session, user_id: int, first_name: str = "Тест") -> None:
    session.add(User(id=user_id, first_name=first_name))
    await session.flush()


async def _seed_daily_stat(session, chat_id: int, user_id: int, stat_date: date, count: int) -> None:
    session.add(DailyStat(chat_id=chat_id, user_id=user_id, stat_date=stat_date, message_count=count))
    await session.flush()


def _today_msk() -> date:
    return datetime.now(MSK).date()


async def _fake_stream_summary(*args, **kwargs):
    for chunk in ("Сегодня ", "обсуждали ", "котиков."):
        yield chunk


# --- count_day_messages ------------------------------------------------------


@pytest.mark.asyncio
async def test_count_day_messages_counts_only_exact_date(session):
    chat_id = -100920000001
    user_id = 700200001
    await _ensure_user(session, user_id)
    today = _today_msk()
    yesterday = today - timedelta(days=1)

    await _seed_daily_stat(session, chat_id, user_id, today, 7)
    await _seed_daily_stat(session, chat_id, user_id, yesterday, 3)

    count = await digest_service.count_day_messages(session, chat_id, today)

    assert count == 7  # только сегодняшняя строка учтена, вчерашняя (3) не примешалась


@pytest.mark.asyncio
async def test_count_day_messages_zero_for_unknown_chat(session):
    count = await digest_service.count_day_messages(session, chat_id=-1, day=_today_msk())
    assert count == 0


# --- build_digest: порог D-03/D-12 -------------------------------------------


@pytest.mark.asyncio
async def test_skips_low_activity_day(session, monkeypatch):
    """D-03/D-12: активность за сегодня ниже digest_min_messages -> build_digest
    возвращает None и НИ РАЗУ не зовёт summary_service.stream_summary (проверка
    через AsyncMock, который бросает, если его дёрнули)."""
    chat_id = -100920000002
    user_id = 700200002
    await _ensure_user(session, user_id)
    today = _today_msk()
    await _seed_daily_stat(session, chat_id, user_id, today, settings.digest_min_messages - 1)

    def _boom(*args, **kwargs):
        raise AssertionError("build_digest не должен звать summary_service при низкой активности")

    from bot.services import summary_service as summary_service_module

    monkeypatch.setattr(summary_service_module, "stream_summary", _boom)

    result = await digest_service.build_digest(session, chat_id)

    assert result is None


@pytest.mark.asyncio
async def test_returns_none_for_zero_activity_day(session):
    chat_id = -100920000003
    result = await digest_service.build_digest(session, chat_id)
    assert result is None


# --- build_digest: три блока при достаточной активности ----------------------


@pytest.mark.asyncio
async def test_returns_three_blocks_when_activity_sufficient(session, monkeypatch):
    chat_id = -100920000004
    user_id = 700200004
    await _ensure_user(session, user_id, first_name="Аня")
    today = _today_msk()
    await _seed_daily_stat(session, chat_id, user_id, today, settings.digest_min_messages)

    from bot.services import mood_service as mood_service_module
    from bot.services import summary_service as summary_service_module

    monkeypatch.setattr(summary_service_module, "stream_summary", _fake_stream_summary)
    monkeypatch.setattr(
        mood_service_module,
        "get_chat_mood",
        AsyncMock(
            return_value={
                "classified_count": 5,
                "avg_sentiment": 0.7,
                "label_shares": {"positive": 0.6, "neutral": 0.2, "negative": 0.2},
            }
        ),
    )
    monkeypatch.setattr(
        mood_service_module,
        "get_chat_toxicity",
        AsyncMock(return_value={"classified_count": 5, "avg_toxicity": 0.1, "toxic_share": 0.0}),
    )

    result = await digest_service.build_digest(session, chat_id)

    assert result is not None
    assert "Сегодня обсуждали котиков." in result  # блок 1: AI-пересказ
    assert "Топ участников дня" in result and "Аня" in result  # блок 2: топ-активные
    assert "Настроение и токсичность дня" in result  # блок 3: mood/toxic
    assert "Позитивных 60%" in result
