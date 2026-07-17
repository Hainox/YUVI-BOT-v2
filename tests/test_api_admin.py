"""Тесты GET /api/v1/admin/summary + GET /api/v1/admin/analytics + is_admin на
GET /api/v1/me (CASINO-03, D-03) — против живого Postgres, тот же
fixture-паттерн, что test_api_economy.py/test_api_feedback.py (ASGITransport +
monkeypatch `telegram_client.get_chat_member_status`).

RED (Task 1): `api/routes/admin.py` ещё не существует (`_discover_routers` не
находит `router`) — все запросы к /api/v1/admin/* вернут 404, а GET /me ещё не
отдаёт is_admin — тесты падают. Реализация — Task 2 (GREEN).

T-04.3-05 (Pitfall 4): каждый /api/v1/admin/* роут ОБЯЗАН использовать
require_admin (не require_membership) — test_requires_admin доказывает это
поведенчески (403 для "member", 200 для "administrator").
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from unittest.mock import AsyncMock
from urllib.parse import urlencode

import pytest
from httpx import ASGITransport
from httpx import AsyncClient
from sqlalchemy.dialects.postgresql import insert as pg_insert

from api import telegram_client
from api.main import app
from bot.config import settings
from common.db.session import engine
from common.db.session import SessionLocal
from common.models.user import User

CHAT_ID = -900510


def _build_init_data(*, user_id: int, bot_token: str | None = None, tamper: bool = False) -> str:
    if bot_token is None:
        bot_token = settings.bot_token
    fields = {
        "auth_date": str(int(time.time())),
        "query_id": "AAABBBCCC",
        "user": json.dumps({"id": user_id, "first_name": "Тест"}),
    }
    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    computed_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()

    full = dict(fields)
    full["hash"] = ("0" * len(computed_hash)) if tamper else computed_hash
    return urlencode(full)


async def _ensure_user(user_id: int, first_name: str = "Тест") -> None:
    async with SessionLocal() as db_session:
        stmt = (
            pg_insert(User)
            .values(id=user_id, first_name=first_name)
            .on_conflict_do_nothing(index_elements=["id"])
        )
        await db_session.execute(stmt)
        await db_session.commit()


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
    app.state.http_client = AsyncMock()
    app.state.redis = None
    yield


@pytest.mark.asyncio
async def test_admin_summary_requires_admin(monkeypatch):
    user_id = 340201
    await _ensure_user(user_id)
    init_data = _build_init_data(user_id=user_id)

    monkeypatch.setattr(telegram_client, "get_chat_member_status", AsyncMock(return_value="member"))
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp_member = await client.get(
            "/api/v1/admin/summary",
            params={"chat_id": CHAT_ID},
            headers={"X-Telegram-Init-Data": init_data},
        )
    assert resp_member.status_code == 403

    monkeypatch.setattr(
        telegram_client, "get_chat_member_status", AsyncMock(return_value="administrator")
    )
    telegram_client.reset_cache()
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp_admin = await client.get(
            "/api/v1/admin/summary",
            params={"chat_id": CHAT_ID},
            headers={"X-Telegram-Init-Data": init_data},
        )
    assert resp_admin.status_code == 200


@pytest.mark.asyncio
async def test_admin_summary_unauthenticated_returns_401():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/v1/admin/summary", params={"chat_id": CHAT_ID})

    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_admin_analytics_requires_admin(monkeypatch):
    user_id = 340202
    await _ensure_user(user_id)
    init_data = _build_init_data(user_id=user_id)

    monkeypatch.setattr(telegram_client, "get_chat_member_status", AsyncMock(return_value="member"))
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp_member = await client.get(
            "/api/v1/admin/analytics",
            params={"chat_id": CHAT_ID},
            headers={"X-Telegram-Init-Data": init_data},
        )
    assert resp_member.status_code == 403

    monkeypatch.setattr(
        telegram_client, "get_chat_member_status", AsyncMock(return_value="administrator")
    )
    telegram_client.reset_cache()
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp_admin = await client.get(
            "/api/v1/admin/analytics",
            params={"chat_id": CHAT_ID},
            headers={"X-Telegram-Init-Data": init_data},
        )
    assert resp_admin.status_code == 200


@pytest.mark.asyncio
async def test_admin_analytics_unauthenticated_returns_401():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/v1/admin/analytics", params={"chat_id": CHAT_ID})

    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_admin_analytics_response_shape(monkeypatch):
    user_id = 340203
    await _ensure_user(user_id)
    init_data = _build_init_data(user_id=user_id)

    monkeypatch.setattr(
        telegram_client, "get_chat_member_status", AsyncMock(return_value="administrator")
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            "/api/v1/admin/analytics",
            params={"chat_id": CHAT_ID, "period": "7d"},
            headers={"X-Telegram-Init-Data": init_data},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert "game_popularity" in body
    assert "turnover" in body
    assert "active_players" in body
    assert isinstance(body["game_popularity"], list)
    assert isinstance(body["turnover"], dict)
    assert isinstance(body["active_players"], list)


@pytest.mark.asyncio
async def test_me_exposes_is_admin_true_for_administrator(monkeypatch):
    user_id = 340204
    await _ensure_user(user_id)
    init_data = _build_init_data(user_id=user_id)

    monkeypatch.setattr(
        telegram_client, "get_chat_member_status", AsyncMock(return_value="administrator")
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            "/api/v1/me",
            params={"chat_id": CHAT_ID},
            headers={"X-Telegram-Init-Data": init_data},
        )

    assert resp.status_code == 200
    assert resp.json()["is_admin"] is True


@pytest.mark.asyncio
async def test_me_exposes_is_admin_false_for_member(monkeypatch):
    user_id = 340205
    await _ensure_user(user_id)
    init_data = _build_init_data(user_id=user_id)

    monkeypatch.setattr(telegram_client, "get_chat_member_status", AsyncMock(return_value="member"))
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            "/api/v1/me",
            params={"chat_id": CHAT_ID},
            headers={"X-Telegram-Init-Data": init_data},
        )

    assert resp.status_code == 200
    assert resp.json()["is_admin"] is False
