"""Тесты D-01: initData HMAC-валидация + membership/admin проверка без aiogram.

Task 1 (api/telegram_client.py): TTL-кэш get_chat_member_status + is_admin_status.
Task 2 (api/deps.py): validate_init_data (HMAC) + extract_init_data +
require_membership/require_admin + 401-маппинг InvalidInitData.

Реальный HTTP полностью замокан (AsyncMock вместо httpx.AsyncClient) — без сети,
без Postgres. reset_cache() вызывается перед каждым тестом автouse-фикстурой,
иначе module-level кэш telegram_client._cache делает тесты порядко-зависимыми.
"""

from __future__ import annotations

from unittest.mock import AsyncMock
from unittest.mock import Mock

import pytest

from api import telegram_client


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
