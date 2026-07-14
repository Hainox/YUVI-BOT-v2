"""Интеграционные тесты bot/services/settings_service.py (AI-08).

Против живого Postgres (фикстура `session` из conftest.py). Каждый тест
вызывает settings_service.clear_cache() в начале — чтобы кэш из одного теста
не просачивался в другой (модульный dict-кэш общий на процесс).
"""

from __future__ import annotations

import pytest

from bot.config import settings
from bot.services import settings_service


@pytest.mark.asyncio
async def test_set_then_get(session):
    """set_setting инвалидирует кэш немедленно: следующий get_setting в той же
    транзакции (без commit) видит новое значение, а не default/старое."""
    settings_service.clear_cache()
    chat_id = -100111

    await settings_service.set_setting(
        session, chat_id, settings_service.KEY_MODEL, "glm-5.2", updated_by_tg_id=42
    )
    value = await settings_service.get_setting(
        session, chat_id, settings_service.KEY_MODEL, default="should-not-be-used"
    )

    assert value == "glm-5.2"


@pytest.mark.asyncio
async def test_default_when_absent(session):
    settings_service.clear_cache()
    chat_id = -100222

    value = await settings_service.get_setting(
        session, chat_id, settings_service.KEY_PROMPT, default="дефолтный промпт"
    )

    assert value == "дефолтный промпт"


@pytest.mark.asyncio
async def test_get_active_model_falls_back_to_env_default(session):
    settings_service.clear_cache()
    chat_id = -100333

    value = await settings_service.get_active_model(session, chat_id)

    assert value == settings.openai_model


@pytest.mark.asyncio
async def test_get_active_prompt_falls_back_to_env_default(session):
    settings_service.clear_cache()
    chat_id = -100444

    value = await settings_service.get_active_prompt(session, chat_id)

    assert value == settings.ai_default_system_prompt


@pytest.mark.asyncio
async def test_set_setting_upserts_on_second_write(session):
    """Второй set_setting с тем же (chat_id, key) обновляет значение
    (on_conflict_do_update), а не падает на дубликате uq_bot_setting."""
    settings_service.clear_cache()
    chat_id = -100555

    await settings_service.set_setting(
        session, chat_id, settings_service.KEY_MODEL, "glm-5.1", updated_by_tg_id=1
    )
    await settings_service.set_setting(
        session, chat_id, settings_service.KEY_MODEL, "glm-5.2", updated_by_tg_id=2
    )
    value = await settings_service.get_setting(
        session, chat_id, settings_service.KEY_MODEL, default="unused"
    )

    assert value == "glm-5.2"
