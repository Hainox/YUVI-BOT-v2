"""Тест /model_show (bot/handlers/ai_admin.py) — с 2026-07-28 показывает ДВЕ
модели раздельно (twin vs остальные AI-функции, см. bot/config.py::
ai_structured_model), а не одну общую строку, как раньше. Мок Message —
форма test_owner_handler.py."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import bot.handlers.ai_admin as ai_admin_handlers
from bot.config import settings
from bot.services import settings_service


def _fake_message(chat_id: int):
    return SimpleNamespace(chat=SimpleNamespace(id=chat_id), answer=AsyncMock())


@pytest.mark.asyncio
async def test_model_show_reports_twin_and_structured_separately(session):
    settings_service.clear_cache()
    chat_id = -100930100

    message = _fake_message(chat_id)
    await ai_admin_handlers.model_show_command(message, session)

    message.answer.assert_awaited_once()
    text = message.answer.await_args.args[0]
    assert settings.openai_model in text
    assert settings.ai_structured_model in text


@pytest.mark.asyncio
async def test_model_show_reflects_shared_override_in_both_lines(session):
    """Override /model_set — один ключ на чат, побеждает ОБА дефолта сразу
    (см. settings_service.get_active_model)."""
    settings_service.clear_cache()
    chat_id = -100930101

    await settings_service.set_setting(
        session, chat_id, settings_service.KEY_MODEL, "deepseek-v4-pro", updated_by_tg_id=1
    )
    message = _fake_message(chat_id)
    await ai_admin_handlers.model_show_command(message, session)

    text = message.answer.await_args.args[0]
    assert text.count("deepseek-v4-pro") == 2
