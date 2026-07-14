"""Тесты D-02: publish_balance -> Redis pub/sub -> SSE event_stream.

Покрывает: доставка publish->подписчик, фильтр по user_id, heartbeat
": ping\\n\\n" при простое, best-effort деградация publish_balance при
падающем/None redis_client, best-effort деградация event_stream, когда
redis_client.pubsub() поднимает исключение.

test_publish_balance_reaches_subscriber и test_event_stream_filters_by_user_id
идут против ЖИВОГО Redis (`REDIS_URL`, сеть `yuvibotv2_default`) — доказывают
реальную доставку через настоящий pub/sub канал. Остальные тесты используют
фейковые redis-подобные объекты, чтобы детерминированно (без ожидания
реального timeout=20.0 внутри event_stream) проверить heartbeat/best-effort
ветки — никаких watch-режимов, все тесты завершаются за секунды.
"""

from __future__ import annotations

import asyncio
import json
import os

import pytest
import redis.asyncio as redis_asyncio

from api.routes.events import event_stream
from bot.services.balance_events import publish_balance

_REDIS_URL = os.environ["REDIS_URL"]


def _disconnect_after(n: int):
    """is_disconnected-callable: False на первые n вызовов, затем True."""
    calls = {"count": 0}

    async def _inner() -> bool:
        calls["count"] += 1
        return calls["count"] > n

    return _inner


async def _never_disconnect() -> bool:
    return False


async def _await_message(pubsub, overall_timeout: float = 6.0):
    """Ждёт первое НЕ-subscribe-подтверждение сообщение до overall_timeout.

    `get_message(ignore_subscribe_messages=True, timeout=X)` читает РОВНО
    одно сырое Redis-сообщение за вызов — если это subscribe-подтверждение,
    возвращает None немедленно (не блокируется дальше до timeout X). Поэтому
    для настоящего ожидания нужен внешний цикл повторных вызовов, как в
    event_stream (api/routes/events.py).
    """
    loop = asyncio.get_event_loop()
    deadline = loop.time() + overall_timeout
    while loop.time() < deadline:
        message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
        if message is not None:
            return message
    return None


class _FakePubSubNoMessages:
    """pubsub-объект, который никогда не получает сообщений (для heartbeat-теста
    без ожидания реального timeout=20.0 внутри event_stream)."""

    async def subscribe(self, channel: str) -> None:
        return None

    async def get_message(self, ignore_subscribe_messages: bool = True, timeout: float = 20.0):
        await asyncio.sleep(0)
        return None

    async def __aenter__(self) -> "_FakePubSubNoMessages":
        return self

    async def __aexit__(self, *exc_info) -> bool:
        return False


class _FakeRedisNoMessages:
    def pubsub(self) -> _FakePubSubNoMessages:
        return _FakePubSubNoMessages()


class _FailingPublishRedis:
    async def publish(self, channel: str, payload: str) -> None:
        raise ConnectionError("redis unreachable")


class _BrokenPubsubRedis:
    def pubsub(self):
        raise ConnectionError("redis down")


@pytest.mark.asyncio
async def test_publish_balance_reaches_subscriber():
    client = redis_asyncio.from_url(_REDIS_URL)
    chat_id = -900001
    try:
        async with client.pubsub() as pubsub:
            await pubsub.subscribe(f"bal:{chat_id}")
            await asyncio.sleep(0.1)  # дать серверу зарегистрировать подписку

            await publish_balance(client, chat_id, user_id=42, balance=1000)

            message = await _await_message(pubsub)
            assert message is not None
            payload = json.loads(message["data"])
            assert payload == {"user_id": 42, "balance": 1000}
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_event_stream_filters_by_user_id():
    client = redis_asyncio.from_url(_REDIS_URL)
    chat_id = -900002

    agen = event_stream(client, chat_id, user_id=1, is_disconnected=_never_disconnect)
    try:

        async def _get_next_data():
            async for chunk in agen:
                if chunk.startswith("data:"):
                    return chunk
            return None

        task = asyncio.create_task(_get_next_data())
        await asyncio.sleep(0.3)  # дать event_stream подписаться

        await publish_balance(client, chat_id, user_id=999, balance=1)  # чужой user_id, должен быть пропущен
        await asyncio.sleep(0.2)
        await publish_balance(client, chat_id, user_id=1, balance=777)  # целевой user_id, должен прийти

        chunk = await asyncio.wait_for(task, timeout=10.0)
        assert chunk is not None
        payload = json.loads(chunk[len("data: ") :])
        assert payload == {"user_id": 1, "balance": 777}
    finally:
        await agen.aclose()
        await client.aclose()


@pytest.mark.asyncio
async def test_event_stream_heartbeat():
    client = _FakeRedisNoMessages()

    chunks = [
        c
        async for c in event_stream(client, chat_id=1, user_id=1, is_disconnected=_disconnect_after(2))
    ]

    assert chunks == [": ping\n\n", ": ping\n\n"]


@pytest.mark.asyncio
async def test_publish_balance_best_effort_without_redis():
    # падающий redis_client.publish — исключение не поднимается наружу
    result = await publish_balance(_FailingPublishRedis(), chat_id=1, user_id=1, balance=10)
    assert result is None

    # redis_client is None — тоже ранний no-op без ошибки
    result_none = await publish_balance(None, chat_id=1, user_id=1, balance=10)
    assert result_none is None


@pytest.mark.asyncio
async def test_event_stream_degrades_without_redis():
    chunks = [
        c
        async for c in event_stream(
            _BrokenPubsubRedis(), chat_id=1, user_id=1, is_disconnected=_never_disconnect
        )
    ]

    assert chunks == []
