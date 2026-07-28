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
async def test_get_active_model_uses_explicit_default_when_no_override(session):
    """default=... (найдено 2026-07-28: kimi-k2.6, дефолт openai_model,
    систематически падает AIEmptyResponseError на строгих промптах — ask/
    card/digest/итд передают default=settings.ai_structured_model) должен
    победить settings.openai_model, когда нет override в БД."""
    settings_service.clear_cache()
    chat_id = -100666

    value = await settings_service.get_active_model(session, chat_id, default=settings.ai_structured_model)

    assert value == settings.ai_structured_model
    assert value != settings.openai_model


@pytest.mark.asyncio
async def test_get_active_model_different_defaults_dont_leak_between_calls(session):
    """Регрессия (найдено 2026-07-28): кэш get_setting раньше хранил уже
    ПОДСТАВЛЕННЫЙ default, а не факт "есть ли override" — из-за этого ПЕРВЫЙ
    вызов get_active_model для чата "залипал" в кэше, и ВТОРОЙ вызов с ДРУГИМ
    default для того же чата получал закэшированный default первого вызова.
    Ровно это ломало /model_show (двойник и остальные AI-функции — разные
    default для одного chat_id) сразу после первого прогона."""
    settings_service.clear_cache()
    chat_id = -100888

    twin_value = await settings_service.get_active_model(session, chat_id)
    structured_value = await settings_service.get_active_model(
        session, chat_id, default=settings.ai_structured_model
    )

    assert twin_value == settings.openai_model
    assert structured_value == settings.ai_structured_model


@pytest.mark.asyncio
async def test_get_active_model_explicit_default_ignored_when_override_set(session):
    """Явный override (/model_set) — ОДИН общий ключ на чат, побеждает ЛЮБОЙ
    default, переданный вызывающим сервисом (twin vs остальные не разделяют
    override, только фолбэк по умолчанию — см. settings_service.get_active_model)."""
    settings_service.clear_cache()
    chat_id = -100777

    await settings_service.set_setting(
        session, chat_id, settings_service.KEY_MODEL, "deepseek-v4-pro", updated_by_tg_id=1
    )
    value = await settings_service.get_active_model(session, chat_id, default=settings.ai_structured_model)

    assert value == "deepseek-v4-pro"


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
