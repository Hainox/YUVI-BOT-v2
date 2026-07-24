"""Тесты GET /api/v1/changelog (api/routes/changelog.py) — против живого
Postgres. Тот же initData-хелпер/фикстуры, что test_api_gacha.py."""

from __future__ import annotations

import hashlib
import hmac
import inspect
import json
import time
from unittest.mock import AsyncMock
from urllib.parse import urlencode

import pytest
from httpx import ASGITransport
from httpx import AsyncClient

from api import telegram_client
from api.main import app
from api.routes import changelog as changelog_route
from bot.config import settings
from bot.services import changelog_service
from common.db.session import engine
from common.db.session import SessionLocal
from common.models.user import User
from sqlalchemy.dialects.postgresql import insert as pg_insert

CHAT_ID = -900601


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
async def test_get_changelog_returns_entries(monkeypatch):
    monkeypatch.setattr(telegram_client, "get_chat_member_status", AsyncMock(return_value="member"))
    user_id = 600301
    await _ensure_user(user_id)
    init_data = _build_init_data(user_id=user_id)

    async with SessionLocal() as db_session:
        await changelog_service.create_entry(db_session, "Тестовое обновление", "Тело записи.")
        await db_session.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            "/api/v1/changelog",
            params={"chat_id": CHAT_ID},
            headers={"X-Telegram-Init-Data": init_data},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert any(e["title"] == "Тестовое обновление" and e["body"] == "Тело записи." for e in body["entries"])


@pytest.mark.asyncio
async def test_get_changelog_missing_init_data_returns_401():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/v1/changelog", params={"chat_id": CHAT_ID})

    assert resp.status_code == 401


def test_changelog_route_composes_service_no_raw_sql():
    """Форма test_api_gacha.py::test_gacha_route_composes_service_no_raw_sql —
    роут не должен содержать SELECT напрямую, только вызов changelog_service."""
    source = inspect.getsource(changelog_route)
    assert "select(" not in source
    assert "ChangelogEntry" not in source
