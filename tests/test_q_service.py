"""Юнит-тесты поиска `/q` без обращения к внешнему LLM."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from bot.services import q_service


def test_keyword_terms_remove_stop_words_and_make_roots():
    terms = q_service._keyword_terms("что советовал по видеокарте @Hoouka")
    assert "что" not in terms
    assert "советовал" not in terms
    assert "видеокар" in terms


def test_extract_author():
    assert q_service._extract_author("@Hoouka что советовал") == "hoouka"
    assert q_service._extract_author("что советовал") is None


@pytest.mark.asyncio
async def test_rewrite_query_parses_three_plain_lines(monkeypatch):
    monkeypatch.setattr(
        q_service.settings_service,
        "get_active_model",
        AsyncMock(return_value="q-model"),
    )
    monkeypatch.setattr(
        q_service.ai_client,
        "complete_with_fallback",
        AsyncMock(return_value="1. взял 4070\nкупил видеокарту\nкакую карту советовали"),
    )

    result = await q_service._rewrite_query(AsyncMock(), -100, "что советовал по видеокарте")

    assert result == ["взял 4070", "купил видеокарту", "какую карту советовали"]


@pytest.mark.asyncio
async def test_rewrite_failure_falls_back_to_original_only(monkeypatch):
    monkeypatch.setattr(
        q_service.settings_service,
        "get_active_model",
        AsyncMock(return_value="q-model"),
    )
    monkeypatch.setattr(
        q_service.ai_client,
        "complete_with_fallback",
        AsyncMock(side_effect=RuntimeError("provider down")),
    )

    assert await q_service._rewrite_query(AsyncMock(), -100, "вопрос") == []


@pytest.mark.asyncio
async def test_answer_embeds_original_and_rewrites_then_uses_second_llm_call(monkeypatch):
    session = AsyncMock()
    monkeypatch.setattr(q_service, "_rewrite_query", AsyncMock(return_value=["вариант 1", "вариант 2"]))
    monkeypatch.setattr(
        q_service.nlp_client,
        "embed_batch",
        AsyncMock(return_value=[[0.0] * 1024] * 3),
    )
    monkeypatch.setattr(q_service, "hybrid_search", AsyncMock(return_value=[]))
    monkeypatch.setattr(q_service, "_expand_with_neighbors", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        q_service.settings_service,
        "get_active_model",
        AsyncMock(return_value="q-model"),
    )
    monkeypatch.setattr(
        q_service.settings_service,
        "get_explicit_language_enabled",
        AsyncMock(return_value=True),
    )
    complete = AsyncMock(return_value="ответ")
    monkeypatch.setattr(q_service.ai_client, "complete_with_fallback", complete)

    result = await q_service.answer(session, -100, "вопрос")

    assert result == "ответ"
    q_service.nlp_client.embed_batch.assert_awaited_once_with(["вопрос", "вариант 1", "вариант 2"])
    assert complete.await_count == 1
    assert "ВОПРОС ПОЛЬЗОВАТЕЛЯ: вопрос" in complete.await_args.args[0][1]["content"]
    assert "Explicit-выражения" in complete.await_args.args[0][0]["content"]


def test_format_context_marks_hits_and_neighbors():
    rows = [
        {
            "id": 1,
            "text": "взял 4070",
            "created_at": __import__("datetime").datetime(2024, 1, 1),
            "username": "Hoouka",
            "first_name": "H",
            "similarity": 0.98,
            "is_hit": True,
        },
        {
            "id": 2,
            "text": "а где карта",
            "created_at": __import__("datetime").datetime(2024, 1, 1, 0, 1),
            "username": "McFreezy",
            "first_name": "M",
            "similarity": None,
            "is_hit": False,
        },
    ]
    formatted = q_service._format_context(rows)
    assert "★[sim=0.98]" in formatted
    assert "·2024" in formatted
    assert "@Hoouka" in formatted
