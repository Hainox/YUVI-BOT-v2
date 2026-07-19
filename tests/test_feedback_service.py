"""Тесты bot/services/feedback_service.py (CASINO-03, D-04; close()/reward —
FEEDBACK-01, D-14) — против живого Postgres через фикстуру `session` из
tests/conftest.py (транзакция-на-тест, join-savepoint режим — тот же
паттерн, что test_economy_service.py).

RED (Task 1, close()/reward кейсы): feedback_service.close ещё не
существует — AttributeError. submit/list_feedback/set_resolved уже
реализованы (06-03/предыдущие волны) и остаются нетронутыми ниже.
Реализация close() — Task 2 (GREEN).
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from bot.config import settings
from bot.services import economy_service
from bot.services import feedback_service
from common.models.feedback import Feedback
from common.models.user import User

CHAT_A = -900401
CHAT_B = -900402


async def _get_feedback_row(session, feedback_id: int) -> Feedback:
    result = await session.execute(select(Feedback).where(Feedback.id == feedback_id))
    return result.scalar_one()


async def _ensure_user(session, user_id: int, first_name: str = "Тест") -> None:
    session.add(User(id=user_id, first_name=first_name))
    await session.flush()


@pytest.mark.asyncio
async def test_submit_inserts_row_with_author(session):
    user_id = 330101
    await _ensure_user(session, user_id)

    await feedback_service.submit(session, CHAT_A, user_id, "bug", "тестовый баг")
    await session.commit()

    rows = await feedback_service.list_feedback(session, CHAT_A)

    matching = [row for row in rows if row["user_id"] == user_id]
    assert len(matching) == 1
    assert matching[0]["category"] == "bug"
    assert matching[0]["text"] == "тестовый баг"
    assert matching[0]["resolved"] is False


@pytest.mark.asyncio
async def test_set_resolved_toggles_flag(session):
    user_id = 330102
    await _ensure_user(session, user_id)

    await feedback_service.submit(session, CHAT_A, user_id, "idea", "тестовая идея")
    await session.commit()

    rows = await feedback_service.list_feedback(session, CHAT_A)
    feedback_id = next(row["id"] for row in rows if row["user_id"] == user_id)

    toggled = await feedback_service.set_resolved(session, CHAT_A, feedback_id, True)
    await session.commit()
    assert toggled is True

    rows = await feedback_service.list_feedback(session, CHAT_A)
    updated = next(row for row in rows if row["id"] == feedback_id)
    assert updated["resolved"] is True

    missing = await feedback_service.set_resolved(session, CHAT_A, 999_999_999, True)
    await session.commit()
    assert missing is False


@pytest.mark.asyncio
async def test_list_scoped_by_chat(session):
    user_id = 330103
    await _ensure_user(session, user_id)

    await feedback_service.submit(session, CHAT_A, user_id, "complaint", "видно только в A")
    await session.commit()

    rows_b = await feedback_service.list_feedback(session, CHAT_B)

    assert all(row["user_id"] != user_id for row in rows_b)


# --- close(): resolved=True + награда по категории (FEEDBACK-01, D-14) ------


@pytest.mark.asyncio
async def test_close_rewards_by_category(session):
    bug_user, idea_user, complaint_user, other_user = 330301, 330302, 330303, 330304
    for user_id in (bug_user, idea_user, complaint_user, other_user):
        await _ensure_user(session, user_id)

    await feedback_service.submit(session, CHAT_A, bug_user, "bug", "баг")
    await feedback_service.submit(session, CHAT_A, idea_user, "idea", "идея")
    await feedback_service.submit(session, CHAT_A, complaint_user, "complaint", "жалоба")
    await feedback_service.submit(session, CHAT_A, other_user, "other", "прочее")
    await session.commit()

    rows = await feedback_service.list_feedback(session, CHAT_A)
    bug_id = next(r["id"] for r in rows if r["user_id"] == bug_user)
    idea_id = next(r["id"] for r in rows if r["user_id"] == idea_user)
    complaint_id = next(r["id"] for r in rows if r["user_id"] == complaint_user)
    other_id = next(r["id"] for r in rows if r["user_id"] == other_user)

    # bug -> +feedback_reward_bug
    balance_before = await economy_service.get_balance(session, CHAT_A, bug_user)
    closed = await feedback_service.close(session, CHAT_A, bug_id)
    await session.commit()
    assert closed is True
    assert await economy_service.get_balance(session, CHAT_A, bug_user) == (
        balance_before + settings.feedback_reward_bug
    )
    bug_row = await _get_feedback_row(session, bug_id)
    assert bug_row.resolved is True
    assert bug_row.reward == settings.feedback_reward_bug
    assert bug_row.rewarded_at is not None

    # idea -> +feedback_reward_idea
    balance_before = await economy_service.get_balance(session, CHAT_A, idea_user)
    closed = await feedback_service.close(session, CHAT_A, idea_id)
    await session.commit()
    assert closed is True
    assert await economy_service.get_balance(session, CHAT_A, idea_user) == (
        balance_before + settings.feedback_reward_idea
    )
    idea_row = await _get_feedback_row(session, idea_id)
    assert idea_row.reward == settings.feedback_reward_idea
    assert idea_row.rewarded_at is not None

    # complaint -> resolved=True, без награды
    balance_before = await economy_service.get_balance(session, CHAT_A, complaint_user)
    closed = await feedback_service.close(session, CHAT_A, complaint_id)
    await session.commit()
    assert closed is True
    assert await economy_service.get_balance(session, CHAT_A, complaint_user) == balance_before
    complaint_row = await _get_feedback_row(session, complaint_id)
    assert complaint_row.resolved is True
    assert complaint_row.reward == 0

    # other -> resolved=True, без награды
    balance_before = await economy_service.get_balance(session, CHAT_A, other_user)
    closed = await feedback_service.close(session, CHAT_A, other_id)
    await session.commit()
    assert closed is True
    assert await economy_service.get_balance(session, CHAT_A, other_user) == balance_before
    other_row = await _get_feedback_row(session, other_id)
    assert other_row.resolved is True
    assert other_row.reward == 0


@pytest.mark.asyncio
async def test_close_idempotent(session):
    user_id = 330305
    await _ensure_user(session, user_id)
    await feedback_service.submit(session, CHAT_A, user_id, "bug", "повторный баг")
    await session.commit()

    rows = await feedback_service.list_feedback(session, CHAT_A)
    feedback_id = next(r["id"] for r in rows if r["user_id"] == user_id)

    balance_before = await economy_service.get_balance(session, CHAT_A, user_id)

    first = await feedback_service.close(session, CHAT_A, feedback_id)
    await session.commit()
    assert first is True
    balance_after_first = await economy_service.get_balance(session, CHAT_A, user_id)
    assert balance_after_first == balance_before + settings.feedback_reward_bug

    second = await feedback_service.close(session, CHAT_A, feedback_id)
    await session.commit()
    assert second is True
    balance_after_second = await economy_service.get_balance(session, CHAT_A, user_id)
    assert balance_after_second == balance_after_first


@pytest.mark.asyncio
async def test_close_missing_id(session):
    closed = await feedback_service.close(session, CHAT_A, 999_999_998)
    await session.commit()
    assert closed is False
