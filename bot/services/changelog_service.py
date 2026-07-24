"""Лента «Что нового» (WHATSNEW-01) — обновления/планы разработки, читаемые
всеми в Mini App, публикуемые ТОЛЬКО владельцем бота (`bot/handlers/owner.py::
post_update_command`, `settings.owner_id`). Глобальная лента (нет chat_id) —
один продукт, одна история изменений на всех чатов.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from common.models.changelog_entry import ChangelogEntry

LIST_LIMIT = 50


async def create_entry(session: AsyncSession, title: str, body: str | None) -> ChangelogEntry:
    """Не коммитит — транзакцию завершает вызывающий (форма economy_service)."""
    entry = ChangelogEntry(title=title, body=body)
    session.add(entry)
    await session.flush()
    return entry


async def list_entries(session: AsyncSession, limit: int = LIST_LIMIT) -> list[ChangelogEntry]:
    """Новые сверху, до `limit` записей (по умолчанию 50 — лента новостей,
    не архив, не нужна пагинация)."""
    rows = (
        await session.execute(
            select(ChangelogEntry).order_by(ChangelogEntry.created_at.desc()).limit(limit)
        )
    ).scalars().all()
    return list(rows)
