"""Тесты bot/services/changelog_service.py (WHATSNEW-01) — create_entry/
list_entries против живого Postgres (форма test_clicker_service.py: `session`
фикстура conftest.py)."""

from __future__ import annotations

import pytest

from bot.services import changelog_service


@pytest.mark.asyncio
async def test_create_entry_with_body(session):
    entry = await changelog_service.create_entry(session, "Новый ростер гачи", "15 героинь.")
    await session.commit()

    assert entry.id is not None
    assert entry.title == "Новый ростер гачи"
    assert entry.body == "15 героинь."


@pytest.mark.asyncio
async def test_create_entry_without_body(session):
    entry = await changelog_service.create_entry(session, "Только заголовок", None)
    await session.commit()

    assert entry.body is None


@pytest.mark.asyncio
async def test_list_entries_newest_first(session):
    first = await changelog_service.create_entry(session, "Первая запись", None)
    second = await changelog_service.create_entry(session, "Вторая запись", None)
    await session.commit()

    entries = await changelog_service.list_entries(session)
    ids_in_order = [e.id for e in entries if e.id in (first.id, second.id)]

    assert ids_in_order == [second.id, first.id]


@pytest.mark.asyncio
async def test_list_entries_respects_limit(session):
    for i in range(5):
        await changelog_service.create_entry(session, f"Запись {i}", None)
    await session.commit()

    entries = await changelog_service.list_entries(session, limit=2)

    assert len(entries) == 2
