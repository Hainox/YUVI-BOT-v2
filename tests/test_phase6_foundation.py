"""Тесты BLOCKING-фундамента фазы 6 (06-01): Settings-смоук, reward/rewarded_at
round-trip на feedback.

Против живого Postgres через фикстуру `session` из tests/conftest.py
(транзакция-на-тест, тот же паттерн, что test_feedback_service.py).
"""

from __future__ import annotations

from datetime import datetime
from datetime import timezone

import pytest
from sqlalchemy import select

from bot.config import settings
from common.models.feedback import Feedback
from common.models.user import User

CHAT_A = -900601


async def _ensure_user(session, user_id: int, first_name: str = "Тест") -> None:
    session.add(User(id=user_id, first_name=first_name))
    await session.flush()


# --- Settings smoke (страховка от дрейфа .env alias'ов) ---------------------


def test_settings_phase6_defaults():
    assert settings.social_poke_cost == 50
    assert settings.social_hug_cost == 50
    assert settings.social_joke_order_cost == 150
    assert settings.social_roast_cost == 250
    assert settings.cobalt_api_url == "http://cobalt:9000/"
    assert settings.mediadl_cost == 50
    assert settings.mediadl_max_mb == 48
    assert settings.feedback_reward_bug == 500
    assert settings.feedback_reward_idea == 300


# --- feedback.reward/rewarded_at round-trip (D-14) ---------------------------


@pytest.mark.asyncio
async def test_feedback_reward_columns_roundtrip(session):
    user_id = 330601
    await _ensure_user(session, user_id)

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    rewarded = Feedback(
        chat_id=CHAT_A,
        user_id=user_id,
        category="bug",
        text="награждённая заявка",
        reward=500,
        rewarded_at=now,
    )
    session.add(rewarded)
    await session.flush()

    fetched = (
        await session.execute(select(Feedback).where(Feedback.id == rewarded.id))
    ).scalar_one()
    assert fetched.reward == 500
    assert fetched.rewarded_at is not None

    unrewarded = Feedback(
        chat_id=CHAT_A,
        user_id=user_id,
        category="idea",
        text="ещё не награждённая заявка",
    )
    session.add(unrewarded)
    await session.flush()

    fetched_unrewarded = (
        await session.execute(select(Feedback).where(Feedback.id == unrewarded.id))
    ).scalar_one()
    assert fetched_unrewarded.reward is None
    assert fetched_unrewarded.rewarded_at is None
