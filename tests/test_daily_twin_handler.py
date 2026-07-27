"""Тесты реакции "дневного двойника" на реплаи (TWIN-03,
bot/handlers/daily_twin.py) — против живого Postgres (фикстура `session`).

Доказывают:
- Не-реплай / реплай-команда (текст с `/`) / реплай на ЧУЖОЕ (не двойника)
  сообщение — хендлер тихо ничего не делает (no-op), не шлёт в чат.
- Реплай ИМЕННО на пост двойника (найден в daily_twin_posts) — генерирует
  контекстный ответ через twin_service.build_twin_reaction, хардкодит
  дисклеймер-префикс "🎭 Двойник дня — {Имя}:" (D-02/Pitfall 8), и
  журналирует СВОЙ ответ тоже (чтобы цепочка реплаев продолжалась).
- Отозванное согласие (TwinConsentError) и AI-фолбэк (TWIN_FALLBACK_TEXT) —
  тихий скип, без ответа в чат (это фон, не команда-по-запросу).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import bot.handlers.daily_twin as daily_twin_handlers
from bot.services import daily_twin_service
from bot.services import twin_service
from common.models.twin_opt_in import TwinOptIn
from common.models.user import User


async def _ensure_user(session, user_id: int, first_name: str = "Тест") -> None:
    session.add(User(id=user_id, first_name=first_name))
    await session.flush()


async def _set_opt_in(session, chat_id: int, user_id: int, status: str) -> None:
    session.add(TwinOptIn(chat_id=chat_id, user_id=user_id, status=status))
    await session.flush()


async def _fake_stream(messages: list[dict], model: str, max_tokens: int) -> AsyncIterator[str]:
    yield "норм чё"


async def _raising_stream(messages: list[dict], model: str, max_tokens: int) -> AsyncIterator[str]:
    raise RuntimeError("Модель вернула только reasoning без ответа")
    yield  # pragma: no cover - unreachable, делает функцию async-генератором


def _fake_message(
    chat_id: int,
    user_id: int,
    text: str | None,
    *,
    reply_to_message_id: int | None,
    sent_message_id: int = 700_001,
):
    reply_to = SimpleNamespace(message_id=reply_to_message_id) if reply_to_message_id is not None else None
    return SimpleNamespace(
        chat=SimpleNamespace(id=chat_id),
        from_user=SimpleNamespace(id=user_id, first_name="Кто-то"),
        text=text,
        reply_to_message=reply_to,
        answer=AsyncMock(return_value=SimpleNamespace(message_id=sent_message_id)),
    )


# --- no-op ветки -------------------------------------------------------------


@pytest.mark.asyncio
async def test_ignores_non_reply(session):
    message = _fake_message(-100941001, 941001, "просто текст", reply_to_message_id=None)

    await daily_twin_handlers.daily_twin_reaction(message, session)

    message.answer.assert_not_awaited()


@pytest.mark.asyncio
async def test_ignores_command_reply(session):
    message = _fake_message(-100941002, 941002, "/twin", reply_to_message_id=800_001)

    await daily_twin_handlers.daily_twin_reaction(message, session)

    message.answer.assert_not_awaited()


@pytest.mark.asyncio
async def test_ignores_non_text_reply(session):
    message = _fake_message(-100941003, 941003, None, reply_to_message_id=800_002)

    await daily_twin_handlers.daily_twin_reaction(message, session)

    message.answer.assert_not_awaited()


@pytest.mark.asyncio
async def test_ignores_reply_to_unrelated_message(session):
    """Реплай на реальное сообщение чата, но это НЕ пост двойника
    (в daily_twin_posts такого telegram_message_id нет)."""
    message = _fake_message(-100941004, 941004, "го ещё раз", reply_to_message_id=800_003)

    await daily_twin_handlers.daily_twin_reaction(message, session)

    message.answer.assert_not_awaited()


# --- реплай на пост двойника ---------------------------------------------------


@pytest.mark.asyncio
async def test_reacts_and_records_own_reply(session, monkeypatch):
    monkeypatch.setattr(twin_service.ai_client, "stream", _fake_stream)

    chat_id = -100941005
    target_id = 941005
    twin_post_message_id = 800_004
    await _ensure_user(session, target_id, "Дима")
    await _set_opt_in(session, chat_id, target_id, "active")
    await daily_twin_service.record_post(session, chat_id, twin_post_message_id, target_id)
    await session.commit()

    message = _fake_message(
        chat_id, 941006, "го ещё раз", reply_to_message_id=twin_post_message_id, sent_message_id=800_005
    )

    await daily_twin_handlers.daily_twin_reaction(message, session)

    message.answer.assert_awaited_once()
    text = message.answer.await_args.args[0]
    assert text.startswith("🎭 Двойник дня — Дима:")
    assert "норм чё" in text

    # Собственный ответ тоже зажурналирован — цепочка реплаев может продолжаться.
    found = await daily_twin_service.find_target_by_post(session, chat_id, 800_005)
    assert found == target_id


@pytest.mark.asyncio
async def test_silent_on_consent_revoked(session, monkeypatch):
    monkeypatch.setattr(twin_service.ai_client, "stream", _fake_stream)

    chat_id = -100941007
    target_id = 941007
    twin_post_message_id = 800_006
    await _ensure_user(session, target_id, "Пауза")
    await _set_opt_in(session, chat_id, target_id, "paused")
    await daily_twin_service.record_post(session, chat_id, twin_post_message_id, target_id)
    await session.commit()

    message = _fake_message(chat_id, 941008, "эй", reply_to_message_id=twin_post_message_id)

    await daily_twin_handlers.daily_twin_reaction(message, session)

    message.answer.assert_not_awaited()


@pytest.mark.asyncio
async def test_silent_on_ai_fallback(session, monkeypatch):
    monkeypatch.setattr(twin_service.ai_client, "stream", _raising_stream)

    chat_id = -100941009
    target_id = 941009
    twin_post_message_id = 800_007
    await _ensure_user(session, target_id, "Молчун")
    await _set_opt_in(session, chat_id, target_id, "active")
    await daily_twin_service.record_post(session, chat_id, twin_post_message_id, target_id)
    await session.commit()

    message = _fake_message(chat_id, 941010, "как дела", reply_to_message_id=twin_post_message_id)

    await daily_twin_handlers.daily_twin_reaction(message, session)

    message.answer.assert_not_awaited()
