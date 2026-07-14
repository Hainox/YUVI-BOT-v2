"""Юнит-тест bot/utils/stream_edit.py (AI-06) — троттлинг + TelegramRetryAfter.

Чистый юнит-тест, Postgres не нужен: Message замокан AsyncMock, agen —
канонический async-генератор строк. Мокаем на уровне Message.edit_text (а не
openai SDK/HTTP), как советует 02-VALIDATION.md для теста AI-06.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from unittest.mock import AsyncMock
from unittest.mock import MagicMock

import pytest
from aiogram.exceptions import TelegramRetryAfter

from bot.utils.stream_edit import stream_into_message


async def _deltas(*parts: str) -> AsyncIterator[str]:
    for part in parts:
        yield part


@pytest.mark.asyncio
async def test_retry_after_backoff():
    """Первая попытка финальной правки ловит TelegramRetryAfter(retry_after=0):
    код ждёт и повторяет edit_text без исключения наружу; итоговый текст
    доставлен полностью."""
    sent_message = AsyncMock()
    retry_exc = TelegramRetryAfter(method=MagicMock(), message="flood", retry_after=0)
    sent_message.edit_text = AsyncMock(side_effect=[retry_exc, None])

    message = AsyncMock()
    message.answer = AsyncMock(return_value=sent_message)

    result = await stream_into_message(message, _deltas("Привет, ", "мир!"), interval=0)

    assert result == "Привет, мир!"
    message.answer.assert_awaited_once_with("⏳ Думаю...")
    assert sent_message.edit_text.await_count == 2


@pytest.mark.asyncio
async def test_final_edit_delivers_full_text_even_with_long_interval():
    """Даже если interval никогда не наступает во время стрима (буфер копится
    без промежуточных правок), после генератора выполняется одна безусловная
    финальная правка с полным текстом (Pitfall 4 — последний хвост не теряется)."""
    sent_message = AsyncMock()
    message = AsyncMock()
    message.answer = AsyncMock(return_value=sent_message)

    result = await stream_into_message(
        message, _deltas("часть один ", "часть два"), interval=9999
    )

    assert result == "часть один часть два"
    sent_message.edit_text.assert_awaited_once_with("часть один часть два")


@pytest.mark.asyncio
async def test_not_modified_error_is_swallowed():
    """TelegramBadRequest с текстом 'message is not modified' не должен всплывать
    наружу — это не ошибка, а сигнал, что буфер не изменился с прошлой правки."""
    from aiogram.exceptions import TelegramBadRequest

    sent_message = AsyncMock()
    not_modified_exc = TelegramBadRequest(method=MagicMock(), message="message is not modified")
    sent_message.edit_text = AsyncMock(side_effect=not_modified_exc)

    message = AsyncMock()
    message.answer = AsyncMock(return_value=sent_message)

    result = await stream_into_message(message, _deltas("текст"), interval=0)

    assert result == "текст"
