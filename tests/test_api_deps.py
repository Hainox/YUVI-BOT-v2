"""Тесты require_membership channel-гейта (HAVD-01, запрошено 2026-07-24) —
доступ к Mini App требует членства и в group chat_id, И подписки на
settings.havd_channel_username. Против живого Postgres (не обязателен для
самого гейта, но GET /api/v1/shop уже зависит от require_membership и ничего
больше не трогает — удобная тонкая точка входа, форма test_api_gacha.py).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from urllib.parse import urlencode

import pytest
from httpx import ASGITransport
from httpx import AsyncClient

from api import telegram_client
from api.main import app
from bot.config import settings
from common.db.session import engine

CHAT_ID = -900801


def _build_init_data(*, user_id: int) -> str:
    fields = {
        "auth_date": str(int(time.time())),
        "query_id": "AAABBBCCC",
        "user": json.dumps({"id": user_id, "first_name": "Тест"}),
    }
    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret_key = hmac.new(b"WebAppData", settings.bot_token.encode(), hashlib.sha256).digest()
    computed_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
    full = dict(fields)
    full["hash"] = computed_hash
    return urlencode(full)


@pytest.fixture(autouse=True)
def _reset_membership_cache():
    telegram_client.reset_cache()
    yield
    telegram_client.reset_cache()


@pytest.fixture(autouse=True)
async def _fresh_engine_per_test():
    await engine.dispose()
    yield
    await engine.dispose()


@pytest.fixture(autouse=True)
def _app_state():
    from unittest.mock import AsyncMock

    app.state.http_client = AsyncMock()
    app.state.redis = None
    yield


async def _get_shop(init_data: str):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get(
            "/api/v1/shop",
            params={"chat_id": CHAT_ID},
            headers={"X-Telegram-Init-Data": init_data},
        )


@pytest.mark.asyncio
async def test_member_of_chat_but_not_subscribed_to_channel_returns_403(monkeypatch):
    async def fake_status(client, bot_token, chat_id, user_id):
        if chat_id == CHAT_ID:
            return "member"
        assert chat_id == settings.havd_channel_username
        return "left"

    monkeypatch.setattr(telegram_client, "get_chat_member_status", fake_status)
    resp = await _get_shop(_build_init_data(user_id=800101))

    assert resp.status_code == 403
    assert resp.json()["detail"] == "not_subscribed_to_channel"


@pytest.mark.asyncio
async def test_member_of_chat_and_subscribed_to_channel_passes(monkeypatch):
    async def fake_status(client, bot_token, chat_id, user_id):
        return "member"

    monkeypatch.setattr(telegram_client, "get_chat_member_status", fake_status)
    resp = await _get_shop(_build_init_data(user_id=800102))

    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_not_member_of_chat_returns_403_before_channel_check(monkeypatch):
    """Групповой гейт остаётся первым — не подменяется новым channel-гейтом."""
    channel_check_called = False

    async def fake_status(client, bot_token, chat_id, user_id):
        nonlocal channel_check_called
        if chat_id == CHAT_ID:
            return "left"
        channel_check_called = True
        return "member"

    monkeypatch.setattr(telegram_client, "get_chat_member_status", fake_status)
    resp = await _get_shop(_build_init_data(user_id=800103))

    assert resp.status_code == 403
    assert resp.json()["detail"] == "not a chat member"
    assert channel_check_called is False
