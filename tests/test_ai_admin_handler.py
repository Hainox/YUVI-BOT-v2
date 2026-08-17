"""Тест /model_show после удаления отдельной модели двойника."""

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
async def test_embed_stats_reports_progress(monkeypatch):
    monkeypatch.setattr(
        ai_admin_handlers.embed_worker,
        "get_embed_stats",
        AsyncMock(return_value=(1000, 750, 250)),
    )
    message = _fake_message(-100930200)
    await ai_admin_handlers.embed_stats_command(message, AsyncMock())

    text = message.answer.await_args.args[0]
    assert "Пересчёт BGE-M3" in text
    assert "750" in text and "250" in text
    assert "75.0%" in text
    assert "ещё досчитываются" in text


@pytest.mark.asyncio
async def test_embed_stats_reports_ready_when_no_pending(monkeypatch):
    monkeypatch.setattr(
        ai_admin_handlers.embed_worker,
        "get_embed_stats",
        AsyncMock(return_value=(500, 500, 0)),
    )
    message = _fake_message(-100930201)
    await ai_admin_handlers.embed_stats_command(message, AsyncMock())

    text = message.answer.await_args.args[0]
    assert "готово" in text
    assert "/q готов: Да" in text


@pytest.mark.asyncio
async def test_model_show_reports_current_ai_model(session):
    settings_service.clear_cache()
    chat_id = -100930100

    message = _fake_message(chat_id)
    await ai_admin_handlers.model_show_command(message, session)

    message.answer.assert_awaited_once()
    text = message.answer.await_args.args[0]
    assert settings.ai_structured_model in text
    assert "двойник" not in text.lower()


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
    assert text.count("deepseek-v4-pro") == 1
