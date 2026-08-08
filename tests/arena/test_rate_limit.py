from __future__ import annotations

import os

os.environ.setdefault("BOT_TOKEN", "test-token")
os.environ.setdefault("CHAT_ID", "-700001")
os.environ.setdefault("OWNER_ID", "1")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost:5432/test")

from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from api.arena_rate_limit import check_arena_rate_limit
from api.arena_rate_limit import enforce_arena_rate_limit
from api.arena_rate_limit import rate_limit_key
from api.deps import AuthContext


class FakeRedis:
    def __init__(self):
        self.counts: dict[str, int] = {}
        self.ttls: dict[str, int] = {}

    async def incr(self, key: str) -> int:
        self.counts[key] = self.counts.get(key, 0) + 1
        return self.counts[key]

    async def expire(self, key: str, seconds: int) -> bool:
        self.ttls[key] = seconds
        return True

    async def ttl(self, key: str) -> int:
        return self.ttls.get(key, -1)


class BrokenRedis:
    async def incr(self, key: str) -> int:
        raise RuntimeError("redis unavailable")


class State:
    redis = None


class App:
    state = State()


def request_with(redis) -> Request:
    App.state.redis = redis
    return Request({"type": "http", "app": App(), "headers": [], "query_string": b""})


@pytest.mark.asyncio
async def test_fixed_window_blocks_after_limit_and_reports_ttl():
    redis = FakeRedis()
    key = "arena:ratelimit:-7:11:submit_action"

    assert (await check_arena_rate_limit(redis, key=key, limit=2, window_seconds=10)).allowed
    assert (await check_arena_rate_limit(redis, key=key, limit=2, window_seconds=10)).allowed
    blocked = await check_arena_rate_limit(redis, key=key, limit=2, window_seconds=10)

    assert blocked.allowed is False
    assert blocked.retry_after == 10
    assert redis.ttls[key] == 10


@pytest.mark.asyncio
async def test_rate_limit_keys_are_isolated_by_verified_actor():
    first = AuthContext(user_id=11, chat_id=-7, status="member")
    second = AuthContext(user_id=12, chat_id=-7, status="member")
    assert rate_limit_key(first, "submit_action") != rate_limit_key(second, "submit_action")

    redis = FakeRedis()
    first_key = rate_limit_key(first, "submit_action")
    second_key = rate_limit_key(second, "submit_action")
    await check_arena_rate_limit(redis, key=first_key, limit=1, window_seconds=10)
    assert (await check_arena_rate_limit(redis, key=second_key, limit=1, window_seconds=10)).allowed


@pytest.mark.asyncio
async def test_enforce_maps_excess_to_429_with_retry_after():
    redis = FakeRedis()
    auth = AuthContext(user_id=11, chat_id=-7, status="member")
    request = request_with(redis)
    await enforce_arena_rate_limit(
        request, auth, operation="create_match", limit=1, window_seconds=8
    )

    with pytest.raises(HTTPException) as error:
        await enforce_arena_rate_limit(
            request, auth, operation="create_match", limit=1, window_seconds=8
        )

    assert error.value.status_code == 429
    assert error.value.headers == {"Retry-After": "8"}


@pytest.mark.asyncio
async def test_rate_limit_fails_open_when_redis_is_unavailable():
    decision = await check_arena_rate_limit(
        BrokenRedis(), key="arena:ratelimit:test", limit=1, window_seconds=10
    )
    assert decision.allowed is True
