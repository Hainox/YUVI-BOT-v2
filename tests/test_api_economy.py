"""Тесты GET /api/v1/me (Task 2, economy.py) — против живого Postgres.

initData собирается тем же алгоритмом, что и `test_api_auth.py::_build_init_data`
(независимая сборка = доказательство, что `require_membership` реально проверяет
подпись, а не пропускает всё). `telegram_client.get_chat_member_status`
монкипатчится (без сети к Telegram) — membership-проверка уже покрыта
`test_api_auth.py`, здесь не переисследуется. Денежная часть (`economy_service.
get_balance`) идёт против ЖИВОГО Postgres через прямой `SessionLocal()`
(тот же engine, что использует сам роут) — не через `session`-фикстуру
conftest.py (её join-savepoint режим не нужен здесь, роут открывает
собственную сессию, независимую от тестовой транзакции).
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
from bot.services import economy_service
from common.db.session import engine
from common.db.session import SessionLocal
from common.models.user import User

CHAT_ID = -900201


def _build_init_data(*, user_id: int, bot_token: str | None = None, tamper: bool = False) -> str:
    """Строит валидный (или намеренно испорченный) initData — форма
    `test_api_auth.py::_build_init_data`, независимая от `api/deps.py`."""
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


async def _get_balance(chat_id: int, user_id: int) -> int:
    async with SessionLocal() as db_session:
        return await economy_service.get_balance(db_session, chat_id, user_id)


@pytest.fixture(autouse=True)
def _reset_membership_cache():
    telegram_client.reset_cache()
    yield
    telegram_client.reset_cache()


@pytest.fixture(autouse=True)
async def _fresh_engine_per_test():
    """`common.db.session.engine` — единственный процесс-глобальный async
    engine (тот же, что использует сам роут через `SessionLocal()`).
    pytest-asyncio даёт КАЖДОМУ тесту свой event loop, а asyncpg-соединения
    привязаны к loop'у, в котором были созданы — без `dispose()` до/после
    каждого теста соединения из loop'а предыдущего теста переживают закрытие
    того loop'а и падают с `RuntimeError: ... attached to a different loop`
    при следующем использовании/закрытии пула."""
    await engine.dispose()
    yield
    await engine.dispose()


@pytest.fixture(autouse=True)
def _app_state():
    """`require_membership` читает `request.app.state.http_client` — не
    существует без запущенного lifespan (ASGITransport не триггерит
    startup/shutdown события). `get_chat_member_status` полностью
    монкипатчится в каждом тесте, так что реальный httpx-клиент не нужен —
    только сам факт наличия атрибута."""
    app.state.http_client = AsyncMock()
    app.state.redis = None
    yield


@pytest.mark.asyncio
async def test_get_me_returns_balance_for_authenticated_user(monkeypatch):
    monkeypatch.setattr(telegram_client, "get_chat_member_status", AsyncMock(return_value="member"))
    user_id = 222101
    await _ensure_user(user_id)
    init_data = _build_init_data(user_id=user_id)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            "/api/v1/me",
            params={"chat_id": CHAT_ID},
            headers={"X-Telegram-Init-Data": init_data},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body["balance"], int)
    expected_balance = await _get_balance(CHAT_ID, user_id)
    assert body["balance"] == expected_balance


@pytest.mark.asyncio
async def test_get_me_unauthenticated_returns_401():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/v1/me", params={"chat_id": CHAT_ID})

    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_get_me_forged_init_data_returns_401(monkeypatch):
    monkeypatch.setattr(telegram_client, "get_chat_member_status", AsyncMock(return_value="member"))
    init_data = _build_init_data(user_id=222102, tamper=True)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            "/api/v1/me",
            params={"chat_id": CHAT_ID},
            headers={"X-Telegram-Init-Data": init_data},
        )

    assert resp.status_code == 401
