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

import asyncio
import hashlib
import hmac
import json
import os
import time
from unittest.mock import AsyncMock
from urllib.parse import urlencode

import pytest
import redis.asyncio as redis_asyncio
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
_REDIS_URL = os.environ["REDIS_URL"]


async def _await_message(pubsub, overall_timeout: float = 6.0):
    """Ждёт первое НЕ-subscribe-подтверждение сообщение до overall_timeout —
    та же форма, что `test_api_events.py::_await_message`."""
    loop = asyncio.get_event_loop()
    deadline = loop.time() + overall_timeout
    while loop.time() < deadline:
        message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
        if message is not None:
            return message
    return None


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


# --- GET /leaderboard, POST /transfer, GET /economy, GET /history (04.2-08) -


@pytest.mark.asyncio
async def test_get_leaderboard_returns_rankings(monkeypatch):
    monkeypatch.setattr(telegram_client, "get_chat_member_status", AsyncMock(return_value="member"))
    chat_id = -900202
    user_id = 222110
    await _ensure_user(user_id)
    await _get_balance(chat_id, user_id)
    init_data = _build_init_data(user_id=user_id)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            "/api/v1/leaderboard",
            params={"chat_id": chat_id},
            headers={"X-Telegram-Init-Data": init_data},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body, list)
    assert any(row["user_id"] == user_id for row in body)


@pytest.mark.asyncio
async def test_get_leaderboard_unauthenticated_returns_401():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/v1/leaderboard", params={"chat_id": -900202})

    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_post_transfer_moves_money_and_ignores_foreign_from_user_in_body(monkeypatch):
    """T-04.2-02 (IDOR): a foreign `from_user_id` smuggled into the body must
    have zero effect — `from_user` is taken exclusively from AuthContext."""
    monkeypatch.setattr(telegram_client, "get_chat_member_status", AsyncMock(return_value="member"))
    chat_id = -900203
    sender, receiver, attacker = 222120, 222121, 222122
    await _ensure_user(sender)
    await _ensure_user(receiver)
    await _ensure_user(attacker)
    await _get_balance(chat_id, sender)
    await _get_balance(chat_id, receiver)
    await _get_balance(chat_id, attacker)
    init_data = _build_init_data(user_id=sender)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/transfer",
            params={"chat_id": chat_id},
            headers={"X-Telegram-Init-Data": init_data},
            json={
                "to_user_id": receiver,
                "amount": 100,
                "ref_id": "test_api_transfer_1",
                "from_user_id": attacker,  # IDOR probe - must be silently ignored
            },
        )

    assert resp.status_code == 200
    assert await _get_balance(chat_id, sender) == 1000 - 100
    assert await _get_balance(chat_id, attacker) == 1000  # untouched


@pytest.mark.asyncio
async def test_post_transfer_publishes_balance_to_both_sender_and_recipient(monkeypatch):
    """WR-02 (04.2-REVIEW): transfer_with_fee credits to_user_id too, so the
    recipient's SSE balance channel must also be fed — previously only the
    sender's balance was re-published, leaving the recipient's Mini App tab
    stale until they triggered their own action. Uses a live Redis pub/sub
    (same pattern as test_api_events.py) to prove real delivery, not just
    that publish_balance was called."""
    monkeypatch.setattr(telegram_client, "get_chat_member_status", AsyncMock(return_value="member"))
    chat_id = -900205
    sender, receiver = 222140, 222141
    await _ensure_user(sender)
    await _ensure_user(receiver)
    await _get_balance(chat_id, sender)
    await _get_balance(chat_id, receiver)
    init_data = _build_init_data(user_id=sender)

    redis_client = redis_asyncio.from_url(_REDIS_URL)
    app.state.redis = redis_client
    try:
        # Both publish_balance calls target the SAME per-chat channel
        # (`bal:{chat_id}`) — subscribers are filtered by user_id in the
        # payload downstream (api/routes/events.py), not by channel. So one
        # subscription receives BOTH messages, in publish order.
        async with redis_client.pubsub() as sub:
            await sub.subscribe(f"bal:{chat_id}")
            await asyncio.sleep(0.1)  # дать серверу зарегистрировать подписку

            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.post(
                    "/api/v1/transfer",
                    params={"chat_id": chat_id},
                    headers={"X-Telegram-Init-Data": init_data},
                    json={"to_user_id": receiver, "amount": 100, "ref_id": "test_api_transfer_sse"},
                )

            assert resp.status_code == 200

            first_message = await _await_message(sub)
            assert first_message is not None
            second_message = await _await_message(sub)
            assert second_message is not None

            payloads = [json.loads(first_message["data"]), json.loads(second_message["data"])]
            user_ids = {p["user_id"] for p in payloads}
            assert user_ids == {sender, receiver}

            receiver_payload = next(p for p in payloads if p["user_id"] == receiver)
            assert receiver_payload["balance"] == await _get_balance(chat_id, receiver)
    finally:
        await redis_client.aclose()
        app.state.redis = None


@pytest.mark.asyncio
async def test_post_transfer_insufficient_funds_returns_400(monkeypatch):
    monkeypatch.setattr(telegram_client, "get_chat_member_status", AsyncMock(return_value="member"))
    chat_id = -900204
    sender, receiver = 222130, 222131
    await _ensure_user(sender)
    await _ensure_user(receiver)
    await _get_balance(chat_id, sender)
    await _get_balance(chat_id, receiver)
    init_data = _build_init_data(user_id=sender)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/transfer",
            params={"chat_id": chat_id},
            headers={"X-Telegram-Init-Data": init_data},
            json={"to_user_id": receiver, "amount": 999_999, "ref_id": "test_api_transfer_insufficient"},
        )

    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_post_transfer_unauthenticated_returns_401():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/transfer",
            params={"chat_id": -900204},
            json={"to_user_id": 1, "amount": 10, "ref_id": "test_api_transfer_401"},
        )

    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_get_economy_returns_chat_summary(monkeypatch):
    monkeypatch.setattr(telegram_client, "get_chat_member_status", AsyncMock(return_value="member"))
    chat_id = -900205
    user_id = 222140
    await _ensure_user(user_id)
    await _get_balance(chat_id, user_id)
    init_data = _build_init_data(user_id=user_id)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            "/api/v1/economy",
            params={"chat_id": chat_id},
            headers={"X-Telegram-Init-Data": init_data},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert "bank_balance" in body
    assert "total_in_circulation" in body
    assert "open_markets_count" in body


@pytest.mark.asyncio
async def test_get_history_returns_auth_users_transactions(monkeypatch):
    monkeypatch.setattr(telegram_client, "get_chat_member_status", AsyncMock(return_value="member"))
    chat_id = -900206
    user_id, other_id = 222150, 222151
    await _ensure_user(user_id)
    await _ensure_user(other_id)
    await _get_balance(chat_id, user_id)
    await _get_balance(chat_id, other_id)
    init_data = _build_init_data(user_id=user_id)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            "/api/v1/history",
            params={"chat_id": chat_id},
            headers={"X-Telegram-Init-Data": init_data},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body, list)
    assert len(body) >= 1
    assert all(row["user_id"] == user_id for row in body)


@pytest.mark.asyncio
async def test_get_history_unauthenticated_returns_401():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/v1/history", params={"chat_id": -900206})

    assert resp.status_code == 401
