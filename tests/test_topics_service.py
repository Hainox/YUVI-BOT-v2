"""Тест bot/services/topics_service.build_topics (AI-05, T-02-26).

Доказывает, что KMeans-конвейер не падает при недостатке данных: k
понижается до фактического числа сэмплов (effective_k = min(k, len(rows))),
а при числе эмбеддингов меньше MIN_SAMPLES функция вовсе не зовёт LLM и
возвращает NO_DATA_MESSAGE. ai_client.stream всегда замокан — реальный
LLM-вызов здесь не тестируется (интеграционный smoke — вне unit-теста).
"""

from __future__ import annotations

import pytest

from bot.services import topics_service
from common.models.message import Message
from common.models.message_embedding import MessageEmbedding
from common.models.user import User

EMBEDDING_DIM = 768


async def _seed_embedded_message(
    session, chat_id: int, user_id: int, telegram_message_id: int, text: str, vector: list[float]
) -> None:
    message = Message(chat_id=chat_id, user_id=user_id, telegram_message_id=telegram_message_id, text=text)
    session.add(message)
    await session.flush()
    session.add(MessageEmbedding(message_id=message.id, chat_id=chat_id, embedding=vector))
    await session.flush()


def _vector(seed: float) -> list[float]:
    """Детерминированный вектор длины 768 — координата 0 выделена, остальные
    нули, чтобы KMeans разводил кластеры предсказуемо и без флейков."""
    vec = [0.0] * EMBEDDING_DIM
    vec[0] = seed
    return vec


async def _ensure_user(session, user_id: int, first_name: str = "Тест") -> None:
    session.add(User(id=user_id, first_name=first_name))
    await session.flush()


@pytest.mark.asyncio
async def test_kmeans_reduces_k_on_small_data(session, monkeypatch):
    """3 сообщения с эмбеддингом, запрошено k=8 (DEFAULT_K) — KMeans должен
    понизить k до 3 и не упасть; build_topics возвращает непустой текст
    подписей (LLM замокана), а не бросает исключение."""
    chat_id = -100920000001
    user_id = 700200001
    await _ensure_user(session, user_id)

    await _seed_embedded_message(session, chat_id, user_id, 1, "привет как дела", _vector(0.0))
    await _seed_embedded_message(session, chat_id, user_id, 2, "го в футбол вечером", _vector(5.0))
    await _seed_embedded_message(session, chat_id, user_id, 3, "кто смотрел новый фильм", _vector(10.0))

    async def fake_stream(messages, model, max_tokens):
        for chunk in ("1: Приветствия\n", "2: Спорт\n", "3: Кино"):
            yield chunk

    monkeypatch.setattr(topics_service.ai_client, "stream", fake_stream)

    result = await topics_service.build_topics(session, chat_id, k=8)

    assert result
    assert result != topics_service.NO_DATA_MESSAGE
    assert "Кино" in result


@pytest.mark.asyncio
async def test_build_topics_returns_no_data_marker_when_not_enough_embeddings(session, monkeypatch):
    """Меньше MIN_SAMPLES (2) сообщений с эмбеддингом — NO_DATA_MESSAGE без
    вызова LLM (ai_client.stream замокан на исключение — тест провалится,
    если build_topics всё же его позовёт)."""
    chat_id = -100920000002
    user_id = 700200002
    await _ensure_user(session, user_id)

    await _seed_embedded_message(session, chat_id, user_id, 1, "единственное сообщение", _vector(0.0))

    def _boom(*args, **kwargs):
        raise AssertionError("build_topics не должен звать ai_client.stream при малом наборе")

    monkeypatch.setattr(topics_service.ai_client, "stream", _boom)

    result = await topics_service.build_topics(session, chat_id, k=8)

    assert result == topics_service.NO_DATA_MESSAGE


@pytest.mark.asyncio
async def test_build_topics_returns_no_data_marker_for_unknown_chat(session):
    """Чат вовсе без эмбеддингов — честный NO_DATA_MESSAGE, не исключение."""
    result = await topics_service.build_topics(session, chat_id=-1, k=8)

    assert result == topics_service.NO_DATA_MESSAGE
