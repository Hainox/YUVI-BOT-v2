"""Тесты POST /api/v1/feedback + GET/PATCH /api/v1/admin/feedback (CASINO-03,
D-04/D-05) — против живого Postgres, тот же fixture-паттерн, что
`test_api_economy.py` (ASGITransport + monkeypatch `telegram_client.
get_chat_member_status`).

RED (Task 1): `api/routes/feedback.py` ещё не существует (`_discover_routers`
не находит `router`) — все запросы вернут 404, тесты падают. Реализация —
Task 2 (GREEN).
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

CHAT_ID = -900410


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
async def test_author_from_auth_context(monkeypatch):
    """T-04.3-01 (IDOR): a foreign user_id/chat_id smuggled into the body
    must have zero effect — author is taken exclusively from AuthContext."""
    monkeypatch.setattr(telegram_client, "get_chat_member_status", AsyncMock(return_value="member"))
    user_id = 330201
    attacker_id = 330299
    await _ensure_user(user_id)
    await _ensure_user(attacker_id)
    init_data = _build_init_data(user_id=user_id)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/feedback",
            params={"chat_id": CHAT_ID},
            headers={"X-Telegram-Init-Data": init_data},
            json={
                "category": "bug",
                "text": "нашёл баг",
                "user_id": attacker_id,
                "chat_id": -1,
            },
        )

    assert resp.status_code == 200

    async with SessionLocal() as session:
        from bot.services import feedback_service

        rows = await feedback_service.list_feedback(session, CHAT_ID)
    matching = [row for row in rows if row["text"] == "нашёл баг"]
    assert len(matching) == 1
    assert matching[0]["user_id"] == user_id


@pytest.mark.asyncio
async def test_post_feedback_unauthenticated_returns_401():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/feedback",
            params={"chat_id": CHAT_ID},
            json={"category": "bug", "text": "без авторизации"},
        )

    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_post_feedback_rejects_bad_category(monkeypatch):
    monkeypatch.setattr(telegram_client, "get_chat_member_status", AsyncMock(return_value="member"))
    user_id = 330202
    await _ensure_user(user_id)
    init_data = _build_init_data(user_id=user_id)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/feedback",
            params={"chat_id": CHAT_ID},
            headers={"X-Telegram-Init-Data": init_data},
            json={"category": "not_a_real_category", "text": "плохая категория"},
        )

    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_post_feedback_rejects_empty_text(monkeypatch):
    monkeypatch.setattr(telegram_client, "get_chat_member_status", AsyncMock(return_value="member"))
    user_id = 330203
    await _ensure_user(user_id)
    init_data = _build_init_data(user_id=user_id)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/feedback",
            params={"chat_id": CHAT_ID},
            headers={"X-Telegram-Init-Data": init_data},
            json={"category": "bug", "text": ""},
        )

    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_admin_feedback_requires_admin(monkeypatch):
    user_id = 330204
    await _ensure_user(user_id)
    init_data = _build_init_data(user_id=user_id)

    monkeypatch.setattr(telegram_client, "get_chat_member_status", AsyncMock(return_value="member"))
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp_member = await client.get(
            "/api/v1/admin/feedback",
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
            "/api/v1/admin/feedback",
            params={"chat_id": CHAT_ID},
            headers={"X-Telegram-Init-Data": init_data},
        )
    assert resp_admin.status_code == 200
    assert isinstance(resp_admin.json(), list)


@pytest.mark.asyncio
async def test_admin_resolve_toggle(monkeypatch):
    monkeypatch.setattr(
        telegram_client, "get_chat_member_status", AsyncMock(return_value="administrator")
    )
    user_id = 330205
    await _ensure_user(user_id)
    init_data = _build_init_data(user_id=user_id)

    from bot.services import feedback_service

    async with SessionLocal() as session:
        await feedback_service.submit(session, CHAT_ID, user_id, "other", "резолви меня")
        await session.commit()

    async with SessionLocal() as session:
        rows = await feedback_service.list_feedback(session, CHAT_ID)
    feedback_id = next(row["id"] for row in rows if row["text"] == "резолви меня")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.patch(
            f"/api/v1/admin/feedback/{feedback_id}",
            params={"chat_id": CHAT_ID},
            headers={"X-Telegram-Init-Data": init_data},
            json={"resolved": True},
        )
    assert resp.status_code == 200

    async with SessionLocal() as session:
        rows = await feedback_service.list_feedback(session, CHAT_ID)
    updated = next(row for row in rows if row["id"] == feedback_id)
    assert updated["resolved"] is True

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp_missing = await client.patch(
            "/api/v1/admin/feedback/999999999",
            params={"chat_id": CHAT_ID},
            headers={"X-Telegram-Init-Data": init_data},
            json={"resolved": True},
        )
    assert resp_missing.status_code == 404
