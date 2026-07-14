"""Интеграционный тест nlp_classifier.run_once против живого Postgres
(фикстура `session` из tests/conftest.py).

Доказывает NLP-02: обрабатываются только строки WHERE nlp_processed_at IS
NULL — уже классифицированная строка не трогается повторно, необработанная
получает nlp_processed_at + записанные scores, а строка с пустым текстом
помечается обработанной без похода в nlp_client (не зацикливается).

nlp_client замокан: тест не ходит в реальный nlp-контейнер (bot пишет в БД
сам, nlp только классифицирует — RESEARCH.md Anti-Patterns).

Запрос run_once не фильтрует по chat_id (бот — для одного чата), поэтому в
общей dev-БД могут уже лежать строки от других тестов/прогонов с
nlp_processed_at IS NULL — тест намеренно не полагается на точное общее
число обработанных строк, а проверяет поведение только на своих собственных
(уникальные telegram_message_id).
"""

from __future__ import annotations

from datetime import datetime
from unittest.mock import AsyncMock
from unittest.mock import patch

import pytest
from sqlalchemy import select

from bot.services import nlp_classifier
from common.models.message import Message
from common.models.user import User  # noqa: F401 - регистрирует таблицу users для FK messages.user_id


def _fake_classify_batch(texts: list[str]) -> list[dict]:
    return [
        {"sentiment_label": "neutral", "sentiment_score": 0.5, "toxicity_score": 0.01}
        for _ in texts
    ]


async def _insert_message(
    session,
    chat_id: int,
    telegram_message_id: int,
    text: str | None,
    nlp_processed_at: datetime | None,
) -> int:
    message = Message(
        chat_id=chat_id,
        telegram_message_id=telegram_message_id,
        user_id=None,
        text=text,
        content_type="text",
        nlp_processed_at=nlp_processed_at,
    )
    session.add(message)
    await session.flush()
    return message.id


@pytest.mark.asyncio
async def test_processes_only_unclassified(session):
    chat_id = -1001

    unprocessed_id = await _insert_message(
        session, chat_id, 900001, "Привет, как дела?", None
    )
    already_processed_at = datetime(2026, 1, 1, 0, 0)
    processed_id = await _insert_message(
        session, chat_id, 900002, "Это уже обработано", already_processed_at
    )
    empty_id = await _insert_message(session, chat_id, 900003, None, None)
    await session.commit()

    with patch(
        "bot.services.nlp_classifier.nlp_client.classify_batch",
        AsyncMock(side_effect=_fake_classify_batch),
    ) as mocked_classify:
        processed_count = await nlp_classifier.run_once(session)

    # Из трёх вставленных строк реально необработанных только две
    # (processed_id намеренно уже помечена обработанной и не должна попасть
    # в выборку) — как минимум эти две должны быть учтены в общем числе.
    assert processed_count >= 2
    mocked_classify.assert_awaited_once()
    called_texts = mocked_classify.call_args.args[0]
    assert "Привет, как дела?" in called_texts
    # Пустой текст не должен уходить в nlp_client.
    assert None not in called_texts

    unprocessed_row = (
        await session.execute(select(Message).where(Message.id == unprocessed_id))
    ).scalar_one()
    assert unprocessed_row.nlp_processed_at is not None
    assert unprocessed_row.sentiment_label == "neutral"
    assert unprocessed_row.sentiment_score == pytest.approx(0.5)
    assert unprocessed_row.toxicity_score == pytest.approx(0.01)

    # Уже обработанная строка не была затронута повторно.
    processed_row = (
        await session.execute(select(Message).where(Message.id == processed_id))
    ).scalar_one()
    assert processed_row.nlp_processed_at == already_processed_at
    assert processed_row.sentiment_label is None
    assert processed_row.sentiment_score is None
    assert processed_row.toxicity_score is None

    # Строка с пустым текстом помечена обработанной, но без scores (T-02-12 —
    # иначе выбиралась бы на каждом тике снова).
    empty_row = (
        await session.execute(select(Message).where(Message.id == empty_id))
    ).scalar_one()
    assert empty_row.nlp_processed_at is not None
    assert empty_row.sentiment_label is None
    assert empty_row.sentiment_score is None
    assert empty_row.toxicity_score is None
