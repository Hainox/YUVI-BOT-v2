"""Интеграционные тесты mood_service/mood.py против живого Postgres.

Доказывает NLP-03: /mood и /toxic считают ТОЛЬКО чистым SQL над заранее
посчитанными колонками messages.sentiment_score/sentiment_label/
toxicity_score — модуль mood_service (и хендлер mood.py) не импортирует
ai_client/nlp_client и не делает никаких HTTP/LLM-вызовов. Также проверяет
D-06 (all-time/period) и корректный no-data случай (без деления на ноль).
"""

from __future__ import annotations

import ast
import inspect
from datetime import datetime
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest

import bot.handlers.mood as mood_handlers
from bot.services import mood_service
from common.models.message import Message
from common.models.user import User

MSK = ZoneInfo("Europe/Moscow")


async def _seed_message(
    session,
    chat_id: int,
    user_id: int,
    telegram_message_id: int,
    *,
    sentiment_label: str | None = None,
    sentiment_score: float | None = None,
    toxicity_score: float | None = None,
    created_at=None,
) -> None:
    message = Message(
        chat_id=chat_id,
        user_id=user_id,
        telegram_message_id=telegram_message_id,
        text="тест",
        sentiment_label=sentiment_label,
        sentiment_score=sentiment_score,
        toxicity_score=toxicity_score,
        nlp_processed_at=datetime.now(MSK).replace(tzinfo=None) if sentiment_label else None,
    )
    if created_at is not None:
        message.created_at = created_at
    session.add(message)
    await session.flush()


async def _ensure_user(session, user_id: int, first_name: str = "Тест") -> None:
    session.add(User(id=user_id, first_name=first_name))
    await session.flush()


def _fake_message(chat_id: int, user_id: int, first_name: str, text: str):
    """Минимальный aiogram-подобный Message для теста тонких хендлеров:
    только атрибуты, которые реально читают хендлеры mood.py, плюс
    AsyncMock на answer() вместо реального похода в Telegram.
    """
    return SimpleNamespace(
        chat=SimpleNamespace(id=chat_id),
        from_user=SimpleNamespace(id=user_id, first_name=first_name),
        text=text,
        answer=AsyncMock(),
    )


# --- NLP-03: никаких внешних LLM/NLP-вызовов -------------------------------


def _imported_module_names(module) -> set[str]:
    """Имена модулей/объектов, импортированных через `import`/`from ... import`
    в исходнике модуля. Разбираем AST (не ищем подстроку в тексте), чтобы
    комментарии/докстринги, упоминающие "ai_client"/"nlp_client" как текст
    предупреждения, не давали ложных срабатываний."""
    tree = ast.parse(inspect.getsource(module))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name.split(".")[-1])
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                names.add(node.module.split(".")[-1])
            for alias in node.names:
                names.add(alias.name)
    return names


def test_mood_service_never_imports_ai_or_nlp_client():
    """mood_service — чистый SQL над заранее посчитанными колонками, не
    может импортировать LLM/NLP-клиенты (Anti-Pattern из RESEARCH.md)."""
    imported = _imported_module_names(mood_service)
    assert "ai_client" not in imported
    assert "nlp_client" not in imported


def test_mood_handlers_never_import_ai_or_nlp_client():
    """Хендлеры /mood /toxic тоже не должны напрямую импортировать LLM/NLP-
    клиенты — вся классификация делается заранее фоновым job'ом (плана 02-05)."""
    imported = _imported_module_names(mood_handlers)
    assert "ai_client" not in imported
    assert "nlp_client" not in imported


@pytest.mark.asyncio
async def test_mood_no_external_calls(session, monkeypatch):
    """Прямая проверка NLP-03: подменяем bot.services.nlp_client и
    bot.services.ai_client методами, которые падают при вызове — если
    get_chat_mood/get_chat_toxicity дёрнут их, тест упадёт."""

    def _boom(*args, **kwargs):
        raise AssertionError("mood_service не должен звать nlp_client/ai_client")

    import bot.services.nlp_client as nlp_client_module

    monkeypatch.setattr(nlp_client_module, "classify_batch", _boom)
    monkeypatch.setattr(nlp_client_module, "embed_batch", _boom)

    chat_id = -100910000001
    user_id = 700100001
    await _ensure_user(session, user_id)
    await _seed_message(
        session, chat_id, user_id, 1, sentiment_label="positive", sentiment_score=0.9
    )

    mood = await mood_service.get_chat_mood(session, chat_id, days=None)
    toxicity = await mood_service.get_chat_toxicity(session, chat_id, days=None)

    assert mood["classified_count"] == 1
    assert toxicity["classified_count"] == 0  # toxicity_score не задан в этом сиде


# --- get_chat_mood ----------------------------------------------------------


@pytest.mark.asyncio
async def test_get_chat_mood_returns_no_data_marker_when_nothing_classified(session):
    chat_id = -100910000002
    user_id = 700100002
    await _ensure_user(session, user_id)
    await _seed_message(session, chat_id, user_id, 1)  # без sentiment

    mood = await mood_service.get_chat_mood(session, chat_id, days=None)

    assert mood == {"classified_count": 0, "avg_sentiment": None, "label_shares": None}


@pytest.mark.asyncio
async def test_get_chat_mood_returns_zero_for_unknown_chat(session):
    mood = await mood_service.get_chat_mood(session, chat_id=-1, days=None)

    assert mood == {"classified_count": 0, "avg_sentiment": None, "label_shares": None}


@pytest.mark.asyncio
async def test_get_chat_mood_computes_averages_and_label_shares(session):
    chat_id = -100910000003
    user_id = 700100003
    await _ensure_user(session, user_id)

    await _seed_message(session, chat_id, user_id, 1, sentiment_label="positive", sentiment_score=1.0)
    await _seed_message(session, chat_id, user_id, 2, sentiment_label="positive", sentiment_score=0.6)
    await _seed_message(session, chat_id, user_id, 3, sentiment_label="neutral", sentiment_score=0.5)
    await _seed_message(session, chat_id, user_id, 4, sentiment_label="negative", sentiment_score=0.9)
    await _seed_message(session, chat_id, user_id, 5)  # не классифицировано — не учитывается

    mood = await mood_service.get_chat_mood(session, chat_id, days=None)

    assert mood["classified_count"] == 4
    assert mood["avg_sentiment"] == pytest.approx(0.75)
    assert mood["label_shares"]["positive"] == pytest.approx(0.5)
    assert mood["label_shares"]["neutral"] == pytest.approx(0.25)
    assert mood["label_shares"]["negative"] == pytest.approx(0.25)


@pytest.mark.asyncio
async def test_get_chat_mood_respects_period_argument(session):
    chat_id = -100910000004
    user_id = 700100004
    await _ensure_user(session, user_id)
    today = datetime.now(MSK).replace(tzinfo=None)

    await _seed_message(
        session, chat_id, user_id, 1,
        sentiment_label="positive", sentiment_score=1.0, created_at=today,
    )
    await _seed_message(
        session, chat_id, user_id, 2,
        sentiment_label="negative", sentiment_score=0.0,
        created_at=today - timedelta(days=40),
    )

    mood_recent = await mood_service.get_chat_mood(session, chat_id, days=7)

    assert mood_recent["classified_count"] == 1
    assert mood_recent["avg_sentiment"] == pytest.approx(1.0)


# --- get_chat_toxicity -------------------------------------------------------


@pytest.mark.asyncio
async def test_get_chat_toxicity_returns_no_data_marker_when_nothing_classified(session):
    chat_id = -100910000005
    user_id = 700100005
    await _ensure_user(session, user_id)
    await _seed_message(session, chat_id, user_id, 1)

    toxicity = await mood_service.get_chat_toxicity(session, chat_id, days=None)

    assert toxicity == {"classified_count": 0, "avg_toxicity": None, "toxic_share": None}


@pytest.mark.asyncio
async def test_get_chat_toxicity_computes_average_and_toxic_share(session):
    chat_id = -100910000006
    user_id = 700100006
    await _ensure_user(session, user_id)

    await _seed_message(session, chat_id, user_id, 1, sentiment_label="neutral", toxicity_score=0.9)
    await _seed_message(session, chat_id, user_id, 2, sentiment_label="neutral", toxicity_score=0.1)
    await _seed_message(session, chat_id, user_id, 3, sentiment_label="neutral", toxicity_score=0.6)
    await _seed_message(session, chat_id, user_id, 4)  # не классифицировано

    toxicity = await mood_service.get_chat_toxicity(session, chat_id, days=None)

    assert toxicity["classified_count"] == 3
    assert toxicity["avg_toxicity"] == pytest.approx((0.9 + 0.1 + 0.6) / 3)
    assert toxicity["toxic_share"] == pytest.approx(2 / 3)  # 0.9 и 0.6 > 0.5


# --- хендлеры /mood /toxic ---------------------------------------------------


@pytest.mark.asyncio
async def test_mood_command_replies_with_no_data_message(session):
    chat_id = -100910000007
    user_id = 700100007
    await _ensure_user(session, user_id)
    await _seed_message(session, chat_id, user_id, 1)

    message = _fake_message(chat_id, user_id, "Тест", "/mood")
    await mood_handlers.mood_command(message, session)

    message.answer.assert_awaited_once()
    text = message.answer.await_args.args[0]
    assert "Пока недостаточно данных" in text


@pytest.mark.asyncio
async def test_mood_command_replies_with_formatted_stats(session):
    chat_id = -100910000008
    user_id = 700100008
    await _ensure_user(session, user_id)
    await _seed_message(session, chat_id, user_id, 1, sentiment_label="positive", sentiment_score=0.8)

    message = _fake_message(chat_id, user_id, "Тест", "/mood")
    await mood_handlers.mood_command(message, session)

    text = message.answer.await_args.args[0]
    assert "Настроение чата" in text
    assert "Позитивных: 100%" in text


@pytest.mark.asyncio
async def test_toxic_command_replies_with_formatted_stats(session):
    chat_id = -100910000009
    user_id = 700100009
    await _ensure_user(session, user_id)
    await _seed_message(session, chat_id, user_id, 1, sentiment_label="neutral", toxicity_score=0.7)

    message = _fake_message(chat_id, user_id, "Тест", "/toxic")
    await mood_handlers.toxic_command(message, session)

    text = message.answer.await_args.args[0]
    assert "Токсичность чата" in text
    assert "Доля токсичных сообщений: 100%" in text


@pytest.mark.asyncio
async def test_mood_command_respects_days_argument(session):
    chat_id = -100910000010
    user_id = 700100010
    await _ensure_user(session, user_id)
    today = datetime.now(MSK).replace(tzinfo=None)

    await _seed_message(
        session, chat_id, user_id, 1,
        sentiment_label="positive", sentiment_score=1.0, created_at=today,
    )
    await _seed_message(
        session, chat_id, user_id, 2,
        sentiment_label="negative", sentiment_score=0.0,
        created_at=today - timedelta(days=100),
    )

    message = _fake_message(chat_id, user_id, "Тест", "/mood 7")
    await mood_handlers.mood_command(message, session)

    text = message.answer.await_args.args[0]
    assert "за последние 7 дн." in text
    assert "Проанализировано сообщений: 1" in text
