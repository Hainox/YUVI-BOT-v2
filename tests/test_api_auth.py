"""Тесты D-01: initData HMAC-валидация + membership/admin проверка без aiogram.

Task 1 (api/telegram_client.py): TTL-кэш get_chat_member_status + is_admin_status.
Task 2 (api/deps.py): validate_init_data (HMAC) + extract_init_data +
require_membership/require_admin + 401-маппинг InvalidInitData.

Реальный HTTP полностью замокан (AsyncMock вместо httpx.AsyncClient) — без сети,
без Postgres. reset_cache() вызывается перед каждым тестом автouse-фикстурой,
иначе module-level кэш telegram_client._cache делает тесты порядко-зависимыми.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import Mock
from urllib.parse import urlencode

import pytest
from fastapi import HTTPException

from api import deps
from api import telegram_client
from api.deps import AuthContext
from api.deps import InvalidInitData
from api.main import handle_invalid_init_data

_TEST_BOT_TOKEN = "test-bot-token"


@pytest.fixture(autouse=True)
def _reset_telegram_client_cache():
    telegram_client.reset_cache()
    yield
    telegram_client.reset_cache()


def _mock_response(status_code: int, status: str = "member") -> Mock:
    resp = Mock()
    resp.status_code = status_code
    resp.json.return_value = {"result": {"status": status}}
    return resp


# --- Task 1: get_chat_member_status TTL-кэш + is_admin_status -------------


@pytest.mark.asyncio
async def test_get_chat_member_status_caches_within_ttl():
    client = AsyncMock()
    client.get.return_value = _mock_response(200, "administrator")

    first = await telegram_client.get_chat_member_status(client, "test-token", -100, 1)
    second = await telegram_client.get_chat_member_status(client, "test-token", -100, 1)

    assert first == "administrator"
    assert second == "administrator"
    client.get.assert_awaited_once()


@pytest.mark.asyncio
async def test_get_chat_member_status_refetches_after_ttl_expiry(monkeypatch):
    client = AsyncMock()
    client.get.return_value = _mock_response(200, "member")

    fake_time = [1000.0]
    monkeypatch.setattr(telegram_client.time, "monotonic", lambda: fake_time[0])

    await telegram_client.get_chat_member_status(client, "test-token", -100, 2)
    fake_time[0] += telegram_client.CACHE_TTL + 1
    await telegram_client.get_chat_member_status(client, "test-token", -100, 2)

    assert client.get.await_count == 2


@pytest.mark.asyncio
async def test_get_chat_member_status_fail_closed_on_non_200():
    client = AsyncMock()
    client.get.return_value = _mock_response(403)

    status = await telegram_client.get_chat_member_status(client, "test-token", -100, 3)

    assert status == "left"


@pytest.mark.asyncio
async def test_get_chat_member_status_fail_closed_on_network_error():
    client = AsyncMock()
    client.get.side_effect = OSError("network down")

    status = await telegram_client.get_chat_member_status(client, "test-token", -100, 4)

    assert status == "left"


def test_is_admin_status_true_for_administrator_and_creator():
    assert telegram_client.is_admin_status("administrator") is True
    assert telegram_client.is_admin_status("creator") is True


def test_is_admin_status_false_for_non_admin_statuses():
    assert telegram_client.is_admin_status("member") is False
    assert telegram_client.is_admin_status("left") is False
    assert telegram_client.is_admin_status("kicked") is False


# --- Task 2: validate_init_data (HMAC) + extract_init_data ----------------


def _build_init_data(
    bot_token: str = _TEST_BOT_TOKEN,
    *,
    user_id: int = 111,
    auth_date: int | None = None,
    tamper_hash: bool = False,
    omit_hash: bool = False,
) -> str:
    """Строит initData-строку по РЕАЛЬНОМУ алгоритму Telegram (тем же, что
    validate_init_data должен проверять) — сравнение сборки/проверки на
    независимой реализации доказывает, что проверка действительно криптографическая."""
    if auth_date is None:
        auth_date = int(time.time())
    fields = {
        "auth_date": str(auth_date),
        "query_id": "AAABBBCCC",
        "user": json.dumps({"id": user_id, "first_name": "Тест"}),
    }
    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    computed_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()

    full = dict(fields)
    if tamper_hash:
        full["hash"] = "0" * len(computed_hash)
    elif not omit_hash:
        full["hash"] = computed_hash
    return urlencode(full)


def test_validate_init_data_valid_signature_returns_parsed_dict():
    init_data = _build_init_data(user_id=555)

    parsed = deps.validate_init_data(init_data, _TEST_BOT_TOKEN, ttl_seconds=86400)

    assert json.loads(parsed["user"])["id"] == 555
    assert "hash" not in parsed


def test_validate_init_data_tampered_hash_raises():
    init_data = _build_init_data(tamper_hash=True)

    with pytest.raises(InvalidInitData):
        deps.validate_init_data(init_data, _TEST_BOT_TOKEN, ttl_seconds=86400)


def test_validate_init_data_no_hash_raises():
    init_data = _build_init_data(omit_hash=True)

    with pytest.raises(InvalidInitData):
        deps.validate_init_data(init_data, _TEST_BOT_TOKEN, ttl_seconds=86400)


def test_validate_init_data_expired_raises():
    stale_auth_date = int(time.time()) - 1000
    init_data = _build_init_data(auth_date=stale_auth_date)

    with pytest.raises(InvalidInitData):
        deps.validate_init_data(init_data, _TEST_BOT_TOKEN, ttl_seconds=100)


def test_validate_init_data_uses_compare_digest_not_equality():
    """T-04-06: hash сравнивается constant-time — grep-гейт на исходнике, но
    поведенчески проверяем, что подмена хотя бы одного символа хэша отвергается
    (не только полная замена, как в test_validate_init_data_tampered_hash_raises)."""
    init_data = _build_init_data(user_id=777)
    fields = dict(kv.split("=", 1) for kv in init_data.split("&"))
    original_hash = fields["hash"]
    flipped = ("1" if original_hash[0] != "1" else "2") + original_hash[1:]
    fields["hash"] = flipped
    tampered_init_data = urlencode(fields)

    with pytest.raises(InvalidInitData):
        deps.validate_init_data(tampered_init_data, _TEST_BOT_TOKEN, ttl_seconds=86400)


def _fake_request(headers: dict | None = None, query_params: dict | None = None, http_client=None):
    app = SimpleNamespace(state=SimpleNamespace(http_client=http_client))
    return SimpleNamespace(headers=headers or {}, query_params=query_params or {}, app=app)


def test_extract_init_data_from_header():
    init_data = _build_init_data()
    request = _fake_request(headers={"X-Telegram-Init-Data": init_data})

    assert deps.extract_init_data(request) == init_data


def test_extract_init_data_from_query():
    init_data = _build_init_data()
    request = _fake_request(query_params={"init_data": init_data})

    assert deps.extract_init_data(request) == init_data


def test_extract_init_data_missing_raises():
    request = _fake_request()

    with pytest.raises(InvalidInitData):
        deps.extract_init_data(request)


# --- Task 2: require_membership / require_admin (IDOR, D-01) --------------


@pytest.mark.asyncio
async def test_require_membership_403_for_left_status(monkeypatch):
    monkeypatch.setattr(telegram_client, "get_chat_member_status", AsyncMock(return_value="left"))
    init_data = _build_init_data(user_id=111)
    request = _fake_request(
        headers={"X-Telegram-Init-Data": init_data},
        query_params={"chat_id": "-100123"},
        http_client=AsyncMock(),
    )

    with pytest.raises(HTTPException) as exc_info:
        await deps.require_membership(request)

    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_require_membership_ok_for_member_returns_auth_context(monkeypatch):
    monkeypatch.setattr(telegram_client, "get_chat_member_status", AsyncMock(return_value="member"))
    init_data = _build_init_data(user_id=222)
    request = _fake_request(
        headers={"X-Telegram-Init-Data": init_data},
        query_params={"chat_id": "-100999"},
        http_client=AsyncMock(),
    )

    auth = await deps.require_membership(request)

    assert auth == AuthContext(user_id=222, chat_id=-100999, status="member")


@pytest.mark.asyncio
async def test_require_membership_ignores_spoofed_user_id_in_query_uses_initdata(monkeypatch):
    """IDOR (T-04-08): user_id должен браться ТОЛЬКО из провалидированного
    initData, даже если атакующий добавит user_id в query."""
    mock_get_status = AsyncMock(return_value="member")
    monkeypatch.setattr(telegram_client, "get_chat_member_status", mock_get_status)
    init_data = _build_init_data(user_id=111)
    request = _fake_request(
        headers={"X-Telegram-Init-Data": init_data},
        query_params={"chat_id": "-100123", "user_id": "999999"},
        http_client=AsyncMock(),
    )

    auth = await deps.require_membership(request)

    assert auth.user_id == 111
    mock_get_status.assert_awaited_once()
    called_user_id = mock_get_status.call_args.args[-1]
    assert called_user_id == 111


@pytest.mark.asyncio
async def test_require_admin_403_for_non_admin_status(monkeypatch):
    monkeypatch.setattr(telegram_client, "get_chat_member_status", AsyncMock(return_value="member"))
    init_data = _build_init_data(user_id=333)
    request = _fake_request(
        headers={"X-Telegram-Init-Data": init_data},
        query_params={"chat_id": "-100123"},
        http_client=AsyncMock(),
    )

    with pytest.raises(HTTPException) as exc_info:
        await deps.require_admin(request)

    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_require_admin_ok_for_administrator_status(monkeypatch):
    monkeypatch.setattr(telegram_client, "get_chat_member_status", AsyncMock(return_value="administrator"))
    init_data = _build_init_data(user_id=444)
    request = _fake_request(
        headers={"X-Telegram-Init-Data": init_data},
        query_params={"chat_id": "-100123"},
        http_client=AsyncMock(),
    )

    auth = await deps.require_admin(request)

    assert auth == AuthContext(user_id=444, chat_id=-100123, status="administrator")


# --- Task 2: InvalidInitData -> 401 (api/main.py exception handler) -------


@pytest.mark.asyncio
async def test_invalid_init_data_maps_to_401_without_leaking_exception_text():
    response = await handle_invalid_init_data(_fake_request(), InvalidInitData("hash mismatch"))

    assert response.status_code == 401
    body = json.loads(bytes(response.body))
    assert body == {"detail": "invalid init data"}
    assert "hash mismatch" not in json.dumps(body)
