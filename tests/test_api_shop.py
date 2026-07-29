"""Тесты POST /api/v1/shop/* (api/routes/shop.py) — против живого Postgres.
Тот же initData-хелпер/фикстуры, что test_api_gacha.py. Фокус: чат-доставка
результата (запрошено 2026-07-24, жалоба "обнять не доходит") —
`telegram_client.send_message` вызывается на успехе, НЕ вызывается на replay
(`text is None`) и НЕ вызывается на self-target/insufficient-funds (400 ДО
любой доставки).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
import uuid
from unittest.mock import AsyncMock
from urllib.parse import urlencode

import pytest
from httpx import ASGITransport
from httpx import AsyncClient

from api import telegram_client
from api.main import app
from bot.config import settings
from bot.services import economy_service
from bot.services import social_service
from bot.services.ai_client import AIEmptyResponseError
from common.db.session import engine
from common.db.session import SessionLocal
from common.models.user import User
from sqlalchemy.dialects.postgresql import insert as pg_insert

CHAT_ID = -900701


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


async def _top_up(chat_id: int, user_id: int, amount: int, ref_id: str) -> None:
    async with SessionLocal() as db_session:
        await economy_service.get_balance(db_session, chat_id, user_id)
        await economy_service.credit(db_session, chat_id, user_id, amount, kind="test_top_up", ref_id=ref_id)
        await db_session.commit()


async def _touch_balance(chat_id: int, user_id: int) -> None:
    """_resolve_target_name (shop.py) join'ит user_balance — цель должна уже
    иметь строку баланса в этом чате (get_balance get-or-creates), иначе 404
    "target not found in this chat", даже если строка users есть."""
    async with SessionLocal() as db_session:
        await economy_service.get_balance(db_session, chat_id, user_id)
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
async def test_hug_delivers_message_to_chat(monkeypatch):
    monkeypatch.setattr(telegram_client, "get_chat_member_status", AsyncMock(return_value="member"))
    send_message_mock = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr(telegram_client, "send_message", send_message_mock)

    actor_id, target_id = 700101, 700102
    await _ensure_user(actor_id, "Актёр")
    await _ensure_user(target_id, "Цель")
    await _touch_balance(CHAT_ID, target_id)
    await _top_up(CHAT_ID, actor_id, 5000, str(uuid.uuid4()))
    init_data = _build_init_data(user_id=actor_id)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/shop/hug",
            params={"chat_id": CHAT_ID},
            headers={"X-Telegram-Init-Data": init_data},
            json={"target_user_id": target_id, "idem_key": str(uuid.uuid4())},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["replayed"] is False

    send_message_mock.assert_awaited_once()
    call_args = send_message_mock.await_args.args
    assert call_args[2] == CHAT_ID  # (client, bot_token, chat_id, text, parse_mode=...)
    assert "Актёр" in call_args[3]
    assert "Цель" in call_args[3]


@pytest.mark.asyncio
async def test_hug_replay_does_not_redeliver_to_chat(monkeypatch):
    monkeypatch.setattr(telegram_client, "get_chat_member_status", AsyncMock(return_value="member"))
    send_message_mock = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr(telegram_client, "send_message", send_message_mock)

    actor_id, target_id = 700103, 700104
    await _ensure_user(actor_id, "Актёр")
    await _ensure_user(target_id, "Цель")
    await _touch_balance(CHAT_ID, target_id)
    await _top_up(CHAT_ID, actor_id, 5000, str(uuid.uuid4()))
    init_data = _build_init_data(user_id=actor_id)
    idem_key = str(uuid.uuid4())

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await client.post(
            "/api/v1/shop/hug",
            params={"chat_id": CHAT_ID},
            headers={"X-Telegram-Init-Data": init_data},
            json={"target_user_id": target_id, "idem_key": idem_key},
        )
        second = await client.post(
            "/api/v1/shop/hug",
            params={"chat_id": CHAT_ID},
            headers={"X-Telegram-Init-Data": init_data},
            json={"target_user_id": target_id, "idem_key": idem_key},
        )

    assert second.json()["replayed"] is True
    send_message_mock.assert_awaited_once()  # только первый раз, не на replay


@pytest.mark.asyncio
async def test_hug_self_target_returns_400_and_skips_delivery(monkeypatch):
    monkeypatch.setattr(telegram_client, "get_chat_member_status", AsyncMock(return_value="member"))
    send_message_mock = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr(telegram_client, "send_message", send_message_mock)

    actor_id = 700105
    await _ensure_user(actor_id, "Актёр")
    await _touch_balance(CHAT_ID, actor_id)
    init_data = _build_init_data(user_id=actor_id)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/shop/hug",
            params={"chat_id": CHAT_ID},
            headers={"X-Telegram-Init-Data": init_data},
            json={"target_user_id": actor_id, "idem_key": str(uuid.uuid4())},
        )

    assert resp.status_code == 400
    send_message_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_hug_telegram_delivery_failure_does_not_break_response(monkeypatch):
    """Best-effort: сбой sendMessage (сетевая ошибка/Telegram недоступен) не
    должен ломать уже совершённое и закоммиченное списание/ответ клиенту."""
    monkeypatch.setattr(telegram_client, "get_chat_member_status", AsyncMock(return_value="member"))
    monkeypatch.setattr(
        telegram_client, "send_message", AsyncMock(return_value={"ok": False, "description": "boom"})
    )

    actor_id, target_id = 700106, 700107
    await _ensure_user(actor_id, "Актёр")
    await _ensure_user(target_id, "Цель")
    await _touch_balance(CHAT_ID, target_id)
    await _top_up(CHAT_ID, actor_id, 5000, str(uuid.uuid4()))
    init_data = _build_init_data(user_id=actor_id)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/shop/hug",
            params={"chat_id": CHAT_ID},
            headers={"X-Telegram-Init-Data": init_data},
            json={"target_user_id": target_id, "idem_key": str(uuid.uuid4())},
        )

    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_roast_ai_empty_response_returns_400_and_skips_delivery(monkeypatch):
    """Прод-инцидент 2026-07-24: модель вернула только reasoning без ответа —
    ai_client поднимает AIEmptyResponseError, роут должен вернуть аккуратный
    400 (не голый 500) и не публиковать ничего в чат; несостоявшееся списание
    откатывается вместе с сессией (та же дисциплина, что у InsufficientFunds/
    InvalidTarget — исключение поднимается ДО commit)."""
    monkeypatch.setattr(telegram_client, "get_chat_member_status", AsyncMock(return_value="member"))
    send_message_mock = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr(telegram_client, "send_message", send_message_mock)

    async def empty_reasoning_stream(messages, model, max_tokens):
        raise AIEmptyResponseError(model)
        yield  # pragma: no cover - делает функцию async generator, не выполняется

    monkeypatch.setattr(social_service.ai_client, "stream", empty_reasoning_stream)

    actor_id, target_id = 700108, 700109
    await _ensure_user(actor_id, "Актёр")
    await _ensure_user(target_id, "Цель")
    await _touch_balance(CHAT_ID, target_id)
    await _top_up(CHAT_ID, actor_id, 5000, str(uuid.uuid4()))
    init_data = _build_init_data(user_id=actor_id)

    async with SessionLocal() as db_session:
        balance_before = await economy_service.get_balance(db_session, CHAT_ID, actor_id)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/shop/roast",
            params={"chat_id": CHAT_ID},
            headers={"X-Telegram-Init-Data": init_data},
            json={"target_user_id": target_id, "idem_key": str(uuid.uuid4())},
        )

    assert resp.status_code == 400
    assert "reasoning" in resp.json()["detail"].lower()
    send_message_mock.assert_not_awaited()

    async with SessionLocal() as db_session:
        balance_after = await economy_service.get_balance(db_session, CHAT_ID, actor_id)
    assert balance_after == balance_before  # списание откатилось вместе с сессией
