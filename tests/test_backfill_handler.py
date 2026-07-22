"""Тесты bot/handlers/backfill.py — сериализация одновременных запусков.

_run_backfill_and_report держит module-level _backfill_lock: Kurigram
Client("yuvi_backfill_session", ...) в backfill_service всегда открывает
ОДИН и тот же файл SQLite-сессии, независимо от chat_id. Без лока два
одновременных /backfill (двойной вызов подряд или параллельно в разных
чатах) бьются за этот файл и падают с sqlite3.OperationalError: database
is locked — воспроизведено на реальном запуске в чате -1002586380924.
Тест — чистый unit (run_backfill замокан), без живого Postgres.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from bot.handlers import backfill


@pytest.mark.asyncio
async def test_concurrent_backfill_runs_are_serialized_not_parallel(monkeypatch):
    active = 0
    max_active = 0

    async def fake_run_backfill(chat_id: int) -> int:
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.05)
        active -= 1
        return 0

    monkeypatch.setattr(backfill.backfill_service, "run_backfill", fake_run_backfill)
    bot = AsyncMock()

    await asyncio.gather(
        backfill._run_backfill_and_report(bot, -1001),
        backfill._run_backfill_and_report(bot, -1002),
    )

    assert max_active == 1
    assert bot.send_message.await_count == 2
