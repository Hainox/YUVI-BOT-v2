"""Тесты bot/services/feedback_service.py (CASINO-03, D-04) — против живого
Postgres через прямой `SessionLocal()` (форма test_api_economy.py — денежных
проверок здесь нет, но тот же engine/сессия-паттерн, что использует и роут).

RED (Task 1): submit/list_feedback/set_resolved ещё не существуют —
`from bot.services import feedback_service` падает ImportError, таблица
`feedback` ещё не создана миграцией 0007. Реализация — Task 2 (GREEN).
"""

from __future__ import annotations

import pytest
from sqlalchemy.dialects.postgresql import insert as pg_insert

from bot.services import feedback_service
from common.db.session import SessionLocal
from common.models.user import User

CHAT_A = -900401
CHAT_B = -900402


async def _ensure_user(user_id: int, first_name: str = "Тест") -> None:
    async with SessionLocal() as db_session:
        stmt = (
            pg_insert(User)
            .values(id=user_id, first_name=first_name)
            .on_conflict_do_nothing(index_elements=["id"])
        )
        await db_session.execute(stmt)
        await db_session.commit()


@pytest.mark.asyncio
async def test_submit_inserts_row_with_author():
    user_id = 330101
    await _ensure_user(user_id)

    async with SessionLocal() as session:
        await feedback_service.submit(session, CHAT_A, user_id, "bug", "тестовый баг")
        await session.commit()

    async with SessionLocal() as session:
        rows = await feedback_service.list_feedback(session, CHAT_A)

    matching = [row for row in rows if row["user_id"] == user_id]
    assert len(matching) == 1
    assert matching[0]["category"] == "bug"
    assert matching[0]["text"] == "тестовый баг"
    assert matching[0]["resolved"] is False


@pytest.mark.asyncio
async def test_set_resolved_toggles_flag():
    user_id = 330102
    await _ensure_user(user_id)

    async with SessionLocal() as session:
        await feedback_service.submit(session, CHAT_A, user_id, "idea", "тестовая идея")
        await session.commit()

    async with SessionLocal() as session:
        rows = await feedback_service.list_feedback(session, CHAT_A)
    feedback_id = next(row["id"] for row in rows if row["user_id"] == user_id)

    async with SessionLocal() as session:
        toggled = await feedback_service.set_resolved(session, CHAT_A, feedback_id, True)
        await session.commit()
    assert toggled is True

    async with SessionLocal() as session:
        rows = await feedback_service.list_feedback(session, CHAT_A)
    updated = next(row for row in rows if row["id"] == feedback_id)
    assert updated["resolved"] is True

    async with SessionLocal() as session:
        missing = await feedback_service.set_resolved(session, CHAT_A, 999_999_999, True)
        await session.commit()
    assert missing is False


@pytest.mark.asyncio
async def test_list_scoped_by_chat():
    user_id = 330103
    await _ensure_user(user_id)

    async with SessionLocal() as session:
        await feedback_service.submit(session, CHAT_A, user_id, "complaint", "видно только в A")
        await session.commit()

    async with SessionLocal() as session:
        rows_b = await feedback_service.list_feedback(session, CHAT_B)

    assert all(row["user_id"] != user_id for row in rows_b)
