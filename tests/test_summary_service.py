"""Юнит-тест bot/services/summary_service.build_context (AI-07).

Чистый юнит-тест над build_context — не требует Postgres (принимает уже
готовый список строк, никакого session/SQL). fetch_recent_texts/stream_summary
здесь не тестируются напрямую (интеграционный SQL и стриминг к LLM покрыты
косвенно через test_stream_edit.py + smoke-чекпоинт плана).
"""

from __future__ import annotations

from bot.services.summary_service import build_context


def test_truncate_from_end():
    """Сумма строк превышает бюджет — build_context отбрасывает самые старые
    (rows передаются в хронологическом порядке: от старых к новым) и сохраняет
    самые свежие; итог по длине не превышает char_budget."""
    rows = [
        {"author": "Аня", "text": "самое старое сообщение из чата, довольно длинное"},
        {"author": "Боря", "text": "второе по старшинству сообщение, тоже не короткое"},
        {"author": "Вера", "text": "предпоследнее сообщение перед самым свежим"},
        {"author": "Гоша", "text": "самое свежее сообщение — должно остаться в контексте"},
    ]
    char_budget = 100

    context = build_context(rows, char_budget)

    assert len(context) <= char_budget
    assert "Гоша" in context  # самое свежее — обязательно сохранено
    assert "Аня" not in context  # самое старое — отброшено первым


def test_build_context_keeps_everything_when_under_budget():
    rows = [
        {"author": "Аня", "text": "привет"},
        {"author": "Боря", "text": "привет-привет"},
    ]

    context = build_context(rows, char_budget=10_000)

    assert "Аня: привет" in context
    assert "Боря: привет-привет" in context


def test_build_context_empty_rows_returns_empty_string():
    assert build_context([], char_budget=1000) == ""
