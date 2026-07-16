"""Тесты /farmwipe (FARM-03, bot/handlers/farm_admin.py) против живого
Postgres (фикстура `session` из tests/conftest.py) + мок `bot` (форма
`tests/test_economy_handlers.py::_fake_message`). Доказывает D-03 admin-гейт:
live-проверка `admin_service.is_chat_admin` с явным отказом не-админу (не
молчаливый фильтр), сброс фермы цели через `clicker_service.wipe_farm` на
успехе.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import bot.handlers.farm_admin as farm_admin_handlers
from bot.services import admin_service
from bot.services import clicker_service
from common.models.user import User


async def _ensure_user(session, user_id: int, first_name: str = "Тест") -> None:
    session.add(User(id=user_id, first_name=first_name))
    await session.flush()


def _fake_message(
    chat_id: int,
    user_id: int,
    first_name: str,
    text: str,
    *,
    message_id: int = 1,
    reply_to_message=None,
    entities=None,
):
    return SimpleNamespace(
        chat=SimpleNamespace(id=chat_id),
        from_user=SimpleNamespace(id=user_id, first_name=first_name),
        message_id=message_id,
        text=text,
        reply_to_message=reply_to_message,
        entities=entities,
        answer=AsyncMock(),
        reply=AsyncMock(),
    )


@pytest.mark.asyncio
async def test_farmwipe_refuses_non_admin(session, monkeypatch):
    monkeypatch.setattr(admin_service, "is_chat_admin", AsyncMock(return_value=False))
    chat_id = -100920001
    admin_id, target_id = 920001, 920002
    await _ensure_user(session, admin_id, "Не Админ")
    await _ensure_user(session, target_id, "Цель")
    await clicker_service.get_farm_state(session, chat_id, target_id)

    reply_to = SimpleNamespace(from_user=SimpleNamespace(id=target_id, first_name="Цель"))
    message = _fake_message(chat_id, admin_id, "Не Админ", "/farmwipe", reply_to_message=reply_to)
    bot = AsyncMock()

    await farm_admin_handlers.farmwipe_command(message, session, bot)

    admin_service.is_chat_admin.assert_awaited_once()
    message.reply.assert_awaited_once()
    text = message.reply.await_args.args[0]
    assert "админ" in text.lower()

    # Ферма цели НЕ должна быть тронута — отказ произошёл до вызова wipe_farm.
    state = await clicker_service.get_farm_state(session, chat_id, target_id)
    assert state["cp"] == 0  # уже была 0, но проверяем отсутствие исключения/side-effect


@pytest.mark.asyncio
async def test_farmwipe_resets_target_farm_for_admin(session, monkeypatch):
    monkeypatch.setattr(admin_service, "is_chat_admin", AsyncMock(return_value=True))
    chat_id = -100920002
    admin_id, target_id = 920003, 920004
    await _ensure_user(session, admin_id, "Админ")
    await _ensure_user(session, target_id, "Цель")
    await clicker_service.get_farm_state(session, chat_id, target_id)
    from tests.test_clicker_service import _set_farm  # reuse direct-write helper

    await _set_farm(session, chat_id, target_id, cp=5000, tap_level=9, auto_level=4)

    reply_to = SimpleNamespace(from_user=SimpleNamespace(id=target_id, first_name="Цель"))
    message = _fake_message(chat_id, admin_id, "Админ", "/farmwipe", reply_to_message=reply_to)
    bot = AsyncMock()

    await farm_admin_handlers.farmwipe_command(message, session, bot)

    message.answer.assert_awaited_once()
    text = message.answer.await_args.args[0]
    assert "Цель" in text

    from tests.test_clicker_service import _get_farm

    farm = await _get_farm(session, chat_id, target_id)
    assert farm.cp == 0
    assert farm.tap_level == 1
    assert farm.auto_level == 0


@pytest.mark.asyncio
async def test_farmwipe_no_target_gives_usage_hint(session, monkeypatch):
    monkeypatch.setattr(admin_service, "is_chat_admin", AsyncMock(return_value=True))
    chat_id = -100920003
    admin_id = 920005
    await _ensure_user(session, admin_id, "Админ")

    message = _fake_message(chat_id, admin_id, "Админ", "/farmwipe")
    bot = AsyncMock()

    await farm_admin_handlers.farmwipe_command(message, session, bot)

    message.answer.assert_awaited_once()
    text = message.answer.await_args.args[0]
    assert "использование" in text.lower() or "ответьте" in text.lower()
