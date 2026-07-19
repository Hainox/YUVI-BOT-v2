"""Тесты AI-двойника `/twin` (TWIN-01/02, AI-SPEC §3, D-01/D-02).

Доказывают:
- consent-гейт (`_check_consent`) отрабатывает ПЕРВОЙ строкой build_twin_reply —
  до card_service.build_portrait и до любого LLM-вызова (Pitfall 5, Critical
  Failure Mode #1); не-opted-in/paused -> TwinConsentError, портрет цели не читается.
- build_twin_reply возвращает СЫРОЙ текст модели, БЕЗ дисклеймер-префикса
  '🤖 Двойник' (D-02) — префикс добавляет хендлер, не сервис.
- twin_command в хендлере ВСЕГДА добавляет '🤖 Двойник {Имя}:' независимо от
  текста, который вернула (замоканная) build_twin_reply (Pitfall 8).
- reasoning-only модель (RuntimeError из ai_client.stream) деградирует в
  TWIN_FALLBACK_TEXT, не падает 500-кой.
- /twin_optin /twin_pause /twin_resume /twin_optout /twin_status пишут/читают
  ТОЛЬКО строку вызывающего (V4, T-05-04) — @arg на команды согласия игнорируется.

Мокаем ai_client.stream той же формой async-generator'а, что и test_card_service.py
(реального похода к OpenCode Go нет — биллинг-блокер, тот же прецедент фазы 2).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import bot.handlers.twin as twin_handlers
from bot.services import twin_service
from common.models.twin_opt_in import TwinOptIn
from common.models.user import User


async def _ensure_user(session, user_id: int, first_name: str = "Тест") -> None:
    session.add(User(id=user_id, first_name=first_name))
    await session.flush()


async def _set_opt_in_row(session, chat_id: int, user_id: int, status: str) -> None:
    session.add(TwinOptIn(chat_id=chat_id, user_id=user_id, status=status))
    await session.flush()


async def _fake_stream(messages: list[dict], model: str, max_tokens: int) -> AsyncIterator[str]:
    for part in ["йо чё как", " погнали"]:
        yield part


async def _raising_stream(messages: list[dict], model: str, max_tokens: int) -> AsyncIterator[str]:
    raise RuntimeError("Модель вернула только reasoning без ответа")
    yield  # pragma: no cover - unreachable, делает функцию async-генератором


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
    """Минимальный aiogram-подобный Message (форма test_economy_handlers.py)."""
    return SimpleNamespace(
        chat=SimpleNamespace(id=chat_id),
        from_user=SimpleNamespace(id=user_id, first_name=first_name),
        message_id=message_id,
        text=text,
        reply_to_message=reply_to_message,
        entities=entities,
        answer=AsyncMock(),
    )


# --- consent-гейт ПЕРВОЙ строкой (Pitfall 5, Critical Failure Mode #1) ------


@pytest.mark.asyncio
async def test_consent_gate_blocks_before_reading_target(session, monkeypatch):
    """Нет строки twin_opt_ins -> TwinConsentError; card_service.build_portrait
    не должен быть вызван вовсе (доказывает, что consent проверен ДО чтения
    данных/портрета цели)."""

    def _boom_portrait(*args, **kwargs):
        raise AssertionError("build_portrait не должен вызываться до проверки consent")

    monkeypatch.setattr(twin_service.card_service, "build_portrait", _boom_portrait)

    chat_id = -1009008001
    target_id = 9008001
    await _ensure_user(session, target_id, "Цель")

    with pytest.raises(twin_service.TwinConsentError):
        await twin_service.build_twin_reply(session, chat_id, target_id, "Цель")


@pytest.mark.asyncio
async def test_consent_gate_blocks_paused(session, monkeypatch):
    def _boom_portrait(*args, **kwargs):
        raise AssertionError("build_portrait не должен вызываться при статусе paused")

    monkeypatch.setattr(twin_service.card_service, "build_portrait", _boom_portrait)

    chat_id = -1009008002
    target_id = 9008002
    await _ensure_user(session, target_id, "Цель")
    await _set_opt_in_row(session, chat_id, target_id, "paused")

    with pytest.raises(twin_service.TwinConsentError):
        await twin_service.build_twin_reply(session, chat_id, target_id, "Цель")


# --- build_twin_reply: сырой текст, БЕЗ префикса (D-02) ----------------------


@pytest.mark.asyncio
async def test_active_returns_raw_text_without_prefix(session, monkeypatch):
    monkeypatch.setattr(twin_service.ai_client, "stream", _fake_stream)

    chat_id = -1009008003
    target_id = 9008003
    await _ensure_user(session, target_id, "Опытный")
    await _set_opt_in_row(session, chat_id, target_id, "active")

    reply = await twin_service.build_twin_reply(session, chat_id, target_id, "Опытный")

    assert reply == "йо чё как погнали"
    assert "🤖" not in reply
    assert "Двойник" not in reply


# --- reasoning-only деградация ------------------------------------------------


@pytest.mark.asyncio
async def test_reasoning_only_degrades(session, monkeypatch):
    monkeypatch.setattr(twin_service.ai_client, "stream", _raising_stream)

    chat_id = -1009008004
    target_id = 9008004
    await _ensure_user(session, target_id, "Молчун")
    await _set_opt_in_row(session, chat_id, target_id, "active")

    reply = await twin_service.build_twin_reply(session, chat_id, target_id, "Молчун")

    assert reply == twin_service.TWIN_FALLBACK_TEXT


# --- opt-in/out/pause/resume state machine -----------------------------------


@pytest.mark.asyncio
async def test_optin_optout_pause_resume(session):
    chat_id = -1009008005
    user_id = 9008005
    await _ensure_user(session, user_id, "Согласный")

    assert await twin_service.get_status(session, chat_id, user_id) is None

    await twin_service.set_opt_in(session, chat_id, user_id, "active")
    await session.commit()
    assert await twin_service.get_status(session, chat_id, user_id) == "active"

    await twin_service.set_opt_in(session, chat_id, user_id, "paused")
    await session.commit()
    assert await twin_service.get_status(session, chat_id, user_id) == "paused"

    await twin_service.set_opt_in(session, chat_id, user_id, "active")
    await session.commit()
    assert await twin_service.get_status(session, chat_id, user_id) == "active"

    deleted = await twin_service.opt_out(session, chat_id, user_id)
    await session.commit()
    assert deleted is True
    assert await twin_service.get_status(session, chat_id, user_id) is None


# --- twin_command (хендлер): дисклеймер ВСЕГДА добавляется (Pitfall 8) ------


@pytest.mark.asyncio
async def test_handler_prepends_disclosure_regardless(session, monkeypatch):
    async def _fake_build_twin_reply(session, chat_id, target_user_id, target_display_name):
        return "любой текст, даже без упоминания двойника"

    monkeypatch.setattr(twin_handlers.twin_service, "build_twin_reply", _fake_build_twin_reply)

    chat_id = -1009008006
    user_id = 9008006
    message = _fake_message(chat_id, user_id, "Сам-На-Себя", "/twin")

    await twin_handlers.twin_command(message, session)

    message.answer.assert_awaited_once()
    text = message.answer.await_args.args[0]
    assert text.startswith("🤖 Двойник ")


@pytest.mark.asyncio
async def test_handler_shows_not_connected_message_on_consent_error(session):
    chat_id = -1009008007
    user_id = 9008007
    message = _fake_message(chat_id, user_id, "Без-Согласия", "/twin")

    await twin_handlers.twin_command(message, session)

    text = message.answer.await_args.args[0]
    assert "не подключил" in text.lower()


# --- команды согласия: пишут ТОЛЬКО message.from_user.id (V4, T-05-04) ------


@pytest.mark.asyncio
async def test_optin_writes_only_caller_row(session):
    chat_id = -1009008008
    caller_id = 9008008
    other_target_id = 9008009
    await _ensure_user(session, caller_id, "Вызывающий")
    await _ensure_user(session, other_target_id, "Другая-цель")

    message = _fake_message(chat_id, caller_id, "Вызывающий", f"/twin_optin @other_target")

    await twin_handlers.twin_optin(message, session)
    await session.commit()

    assert await twin_service.get_status(session, chat_id, caller_id) == "active"
    assert await twin_service.get_status(session, chat_id, other_target_id) is None


@pytest.mark.asyncio
async def test_pause_resume_optout_act_only_on_caller(session):
    chat_id = -1009008010
    caller_id = 9008010
    await _ensure_user(session, caller_id, "Одиночка")

    optin_message = _fake_message(chat_id, caller_id, "Одиночка", "/twin_optin")
    await twin_handlers.twin_optin(optin_message, session)
    await session.commit()
    assert await twin_service.get_status(session, chat_id, caller_id) == "active"

    pause_message = _fake_message(chat_id, caller_id, "Одиночка", "/twin_pause")
    await twin_handlers.twin_pause(pause_message, session)
    await session.commit()
    assert await twin_service.get_status(session, chat_id, caller_id) == "paused"

    resume_message = _fake_message(chat_id, caller_id, "Одиночка", "/twin_resume")
    await twin_handlers.twin_resume(resume_message, session)
    await session.commit()
    assert await twin_service.get_status(session, chat_id, caller_id) == "active"

    status_message = _fake_message(chat_id, caller_id, "Одиночка", "/twin_status")
    await twin_handlers.twin_status(status_message, session)
    status_text = status_message.answer.await_args.args[0]
    assert "активен" in status_text.lower()

    optout_message = _fake_message(chat_id, caller_id, "Одиночка", "/twin_optout")
    await twin_handlers.twin_optout(optout_message, session)
    await session.commit()
    assert await twin_service.get_status(session, chat_id, caller_id) is None
