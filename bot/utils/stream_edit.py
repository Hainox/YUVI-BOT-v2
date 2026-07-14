"""Троттлинг-хелпер: стримит текст из async-генератора в ОДНО сообщение
Telegram (AI-06).

Редактирует сообщение не чаще interval секунд (по умолчанию
settings.ai_stream_edit_interval_sec, RESEARCH.md Pitfall 4 — Telegram
жёстче лимитирует editMessageText, чем отправку новых сообщений) и только
если буфер вырос заметно (>40 симв.) — иначе мелкие правки быстро упрутся в
флуд-лимит. После генератора всегда делаем одну финальную безусловную
правку, чтобы последний хвост текста не потерялся из-за интервала.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator

from aiogram.exceptions import TelegramBadRequest
from aiogram.exceptions import TelegramRetryAfter
from aiogram.types import Message

from bot.config import settings

_MIN_DELTA_CHARS = 40


async def stream_into_message(
    message: Message,
    agen: AsyncIterator[str],
    interval: float | None = None,
) -> str:
    """Стримит agen в одно сообщение-ответ на message, возвращает полный текст."""
    edit_interval = interval if interval is not None else settings.ai_stream_edit_interval_sec

    sent = await message.answer("⏳ Думаю...")
    buffer = ""
    last_sent = ""
    last_edit_at = time.monotonic()

    async for delta in agen:
        buffer += delta
        now = time.monotonic()
        if now - last_edit_at < edit_interval:
            continue
        if len(buffer) - len(last_sent) < _MIN_DELTA_CHARS:
            continue
        await _safe_edit(sent, buffer)
        last_sent = buffer
        last_edit_at = now

    await _safe_edit(sent, buffer or "Не удалось получить ответ.")
    return buffer


async def _safe_edit(msg: Message, text: str) -> None:
    """Правит сообщение, обрезая до лимита Telegram и переживая флуд-контроль.

    TelegramRetryAfter — ждём ровно retry_after секунд и повторяем один раз.
    "message is not modified" — не ошибка (буфер не изменился с прошлой
    правки), глотаем; любой другой TelegramBadRequest пробрасываем дальше.
    """
    chunk = text[: settings.ai_max_chars_per_message]
    try:
        await msg.edit_text(chunk)
    except TelegramRetryAfter as exc:
        await asyncio.sleep(exc.retry_after)
        await msg.edit_text(chunk)
    except TelegramBadRequest as exc:
        if "message is not modified" not in str(exc):
            raise
