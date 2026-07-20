"""Тесты POST /api/v1/donate (STARS-01, D-10 второй UI-вход из Mini App) —
тот же fixture-паттерн ASGITransport + monkeypatch telegram_client, что
`test_api_feedback.py` (единственный источник правды для auth/membership
моков в этом проекте).

RED (Task 1): `api/routes/donate.py` ещё не существует (`_discover_routers`
не находит `router`) и `api/telegram_client.py::send_invoice` не существует —
все запросы вернут 404/AttributeError, тесты падают. Реализация — там же в
Task 1 (GREEN).
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

CHAT_ID = -900660


def _build_init_data(*, user_id: int, bot_token: str | None = None) -> str:
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
async def test_author_from_auth_context(monkeypatch):
    """T-06-17 (IDOR): posторонний user_id/chat_id, подсунутый в тело, не
    должен иметь никакого эффекта — send_invoice вызывается с chat_id/
    user_id ТОЛЬКО из AuthContext (initData), не из тела запроса."""
    monkeypatch.setattr(telegram_client, "get_chat_member_status", AsyncMock(return_value="member"))
    send_invoice_mock = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr(telegram_client, "send_invoice", send_invoice_mock)

    user_id = 900661
    attacker_id = 900699
    await _ensure_user(user_id)
    init_data = _build_init_data(user_id=user_id)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/donate",
            params={"chat_id": CHAT_ID},
            headers={"X-Telegram-Init-Data": init_data},
            json={"stars": 5, "user_id": attacker_id, "chat_id": -1},
        )

    assert resp.status_code == 200
    send_invoice_mock.assert_awaited_once()
    call_args = send_invoice_mock.await_args.args
    # (client, bot_token, chat_id, title, description, payload, prices)
    assert call_args[2] == CHAT_ID  # NOT the attacker-supplied -1
    assert call_args[5] == f"stars_donate:{user_id}"  # NOT stars_donate:{attacker_id}


@pytest.mark.asyncio
async def test_donate_unauthenticated_returns_401():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/donate",
            params={"chat_id": CHAT_ID},
            json={"stars": 5},
        )

    assert resp.status_code == 401


@pytest.mark.asyncio
@pytest.mark.parametrize("stars", [0, -3])
async def test_donate_rejects_nonpositive_stars(monkeypatch, stars):
    monkeypatch.setattr(telegram_client, "get_chat_member_status", AsyncMock(return_value="member"))
    user_id = 900662
    await _ensure_user(user_id)
    init_data = _build_init_data(user_id=user_id)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/donate",
            params={"chat_id": CHAT_ID},
            headers={"X-Telegram-Init-Data": init_data},
            json={"stars": stars},
        )

    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_donate_large_stars_allowed(monkeypatch):
    """D-12: без верхнего предела — крупное число звёзд проходит валидацию
    (Telegram сам ограничивает оплату на своей стороне)."""
    monkeypatch.setattr(telegram_client, "get_chat_member_status", AsyncMock(return_value="member"))
    monkeypatch.setattr(telegram_client, "send_invoice", AsyncMock(return_value={"ok": True}))
    user_id = 900663
    await _ensure_user(user_id)
    init_data = _build_init_data(user_id=user_id)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/donate",
            params={"chat_id": CHAT_ID},
            headers={"X-Telegram-Init-Data": init_data},
            json={"stars": 100000},
        )

    assert resp.status_code == 200
