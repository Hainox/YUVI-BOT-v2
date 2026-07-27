"""Тесты bot/services/lurker_service.py (LURKER-01) — ежедневный мемный
автопост про Дениску PC Nation и Димочку Yuvi.

Форма test_social_service.py/test_twin_service.py: ai_client.stream всегда
замокан (реального LLM-вызова здесь не тестируем). Доказывает:
- системный промпт содержит оба имени мема и запрет на «грустный» тон.
- #мем дописывается детерминированно самим сервисом, а не доверяется модели.
- пустой ответ модели (после .strip()) -> "" целиком, без одинокого "#мем"
  (вызывающая job'а должна пропустить пост в этот день, не отправлять пустышку).
"""

from __future__ import annotations

import pytest

from bot.services import lurker_service

CHAT_ID = -900801001


@pytest.mark.asyncio
async def test_build_daily_message_appends_hashtag(session, monkeypatch):
    captured_messages: list[dict] = []

    async def capturing_stream(messages, model, max_tokens):
        captured_messages.extend(messages)
        for chunk in ("Сегодняшняя подколка", " про наблюдателей."):
            yield chunk

    monkeypatch.setattr(lurker_service.ai_client, "stream", capturing_stream)

    text = await lurker_service.build_daily_message(session, CHAT_ID)

    assert text == "Сегодняшняя подколка про наблюдателей.\n\n#мем"

    system_prompt = captured_messages[0]["content"]
    assert "PC Nation" in system_prompt
    assert "Yuvi" in system_prompt
    assert "нам грустно" in system_prompt or "грусть" in system_prompt


@pytest.mark.asyncio
async def test_build_daily_message_empty_response_returns_empty(session, monkeypatch):
    async def empty_stream(messages, model, max_tokens):
        return
        yield  # pragma: no cover - unreachable, делает функцию async-генератором

    monkeypatch.setattr(lurker_service.ai_client, "stream", empty_stream)

    text = await lurker_service.build_daily_message(session, CHAT_ID)

    assert text == ""
