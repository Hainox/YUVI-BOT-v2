"""Юнит-тесты bot/services/ask_service.py (AI-04) — RRF-объединение и
честный отказ D-05.

_reciprocal_rank_fusion — чистая функция, тестируется на синтетических
ранг-листах (SimpleNamespace с .id), Postgres не нужен. test_ask_refuses_when_no_match
мокает hybrid_search/nlp_client.embed_batch на уровне функций модуля (а не
БД/HTTP) и проверяет, что при отсутствии релевантных результатов answer
возвращает фразу отказа и НЕ зовёт ai_client.stream (02-VALIDATION.md).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import patch

import pytest

from bot.services import ask_service
from bot.services.ask_service import REFUSAL_MESSAGE
from bot.services.ask_service import _reciprocal_rank_fusion


def test_reciprocal_rank_fusion():
    """id=2 встречается в обоих списках (rank2 в векторном, rank1 в
    лексическом) -> должен оказаться первым с корректным суммарным скором
    по формуле 1/(k+rank)."""
    vector_rows = [SimpleNamespace(id=1), SimpleNamespace(id=2), SimpleNamespace(id=3)]
    lexical_rows = [SimpleNamespace(id=2), SimpleNamespace(id=4)]

    result = _reciprocal_rank_fusion(vector_rows, lexical_rows, k=60)
    scores = dict(result)

    ids_in_order = [item[0] for item in result]
    assert ids_in_order[0] == 2

    expected_score_2 = 1 / (60 + 2) + 1 / (60 + 1)  # rank2 в vector_rows + rank1 в lexical_rows
    assert scores[2] == pytest.approx(expected_score_2)

    expected_score_1 = 1 / (60 + 1)  # только в vector_rows, rank1
    assert scores[1] == pytest.approx(expected_score_1)

    # id=1 (только вектор, rank1) и id=4 (только лексика, rank2) оба встречаются
    # ровно один раз -> суммарный скор id=1 (rank1) больше скора id=4 (rank2)
    assert scores[1] > scores[4]


def test_reciprocal_rank_fusion_empty_lists_returns_empty():
    assert _reciprocal_rank_fusion([], [], k=60) == []


@pytest.mark.asyncio
async def test_ask_refuses_when_no_match():
    """hybrid_search вернул пустой список -> answer возвращает REFUSAL_MESSAGE
    и не вызывает ai_client.stream (D-05: честный отказ без LLM)."""
    with (
        patch("bot.services.ask_service.nlp_client.embed_batch", new=AsyncMock(return_value=[[0.1] * 768])),
        patch("bot.services.ask_service.hybrid_search", new=AsyncMock(return_value=[])),
        patch("bot.services.ask_service.ai_client.stream") as mock_stream,
    ):
        result = await ask_service.answer(session=AsyncMock(), chat_id=-100, question="о чём спорили вчера?")

    assert result == REFUSAL_MESSAGE
    mock_stream.assert_not_called()


@pytest.mark.asyncio
async def test_ask_refuses_when_top_result_below_relevance_threshold():
    """Есть результаты, но лучший не найден лексикой и cosine-дистанция хуже
    порога -> тоже честный отказ, без вызова LLM (A2 порог D-05)."""
    weak_results = [
        {"id": 1, "text": "что-то отдалённо похожее", "score": 0.01, "cosine_distance": 0.9, "in_lexical": False}
    ]
    with (
        patch("bot.services.ask_service.nlp_client.embed_batch", new=AsyncMock(return_value=[[0.1] * 768])),
        patch("bot.services.ask_service.hybrid_search", new=AsyncMock(return_value=weak_results)),
        patch("bot.services.ask_service.ai_client.stream") as mock_stream,
    ):
        result = await ask_service.answer(session=AsyncMock(), chat_id=-100, question="что-то")

    assert result == REFUSAL_MESSAGE
    mock_stream.assert_not_called()
