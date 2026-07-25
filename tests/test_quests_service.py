"""Интеграционные тесты quests_service (QUEST-01) против живого Postgres
(фикстура `session` из tests/conftest.py — форма test_clicker_service.py /
test_awards_service.py: `_ensure_user`, сидинг через `session.add(...)` +
`session.flush()`, `monkeypatch` на `quests_service._today_msk` для контроля
MSK-дня, без моков).

Покрывает:
- Все 6 квестов по отдельности: минимальные данные под ОДНО условие ->
  `claim_quests` -> строка `quest_completions` + `economy_tx`
  (kind=f"quest_{key}") с наградой, которая совпадает с `QuestDef.reward`.
- Идемпотентность: повторный `claim_quests` в тот же MSK-день не платит
  повторно (`on_conflict_do_nothing` на UNIQUE(chat_id, user_id, quest_key,
  day_msk) в самом `quests_service.claim_quests`).
- Смена MSK-дня (тот же seam, что `awards_service._today_msk` в
  test_awards_service.py): день входит в UNIQUE-ключ `quest_completions',
  поэтому тот же квест доступен для клейма заново на следующий день.
- `get_total_completions` — кумулятивный счётчик поперёк ключей и дней.
- `get_quest_status` — `done`/`claimed` корректно отражают прогресс ДО и
  ПОСЛЕ `claim_quests`.
"""

from __future__ import annotations

from datetime import date
from datetime import datetime
from datetime import time

import pytest
from sqlalchemy import func
from sqlalchemy import select

from bot.services import quests_service
from common.models.daily_stat import DailyStat
from common.models.economy_tx import EconomyTx
from common.models.quest_completion import QuestCompletion
from common.models.user import User

TEST_DAY = date(2026, 7, 20)
DAY_2 = date(2026, 7, 21)

_REWARDS: dict[str, int] = {q.key: q.reward for q in quests_service.QUESTS}


# --- Хелперы (форма test_clicker_service.py / test_awards_service.py) --------


async def _ensure_user(session, user_id: int, first_name: str = "Тест") -> None:
    session.add(User(id=user_id, first_name=first_name))
    await session.flush()


async def _seed_daily_stat(
    session,
    chat_id: int,
    user_id: int,
    stat_date: date,
    *,
    message_count: int = 0,
    photo_count: int = 0,
) -> None:
    session.add(
        DailyStat(
            chat_id=chat_id,
            user_id=user_id,
            stat_date=stat_date,
            message_count=message_count,
            photo_count=photo_count,
        )
    )
    await session.flush()


def _msk_noon_utc_naive(day: date) -> datetime:
    """Naive UTC-datetime «в полдень» MSK-дня `day` — та же конвертация, что
    `quests_service._today_msk_start_utc`, но для произвольного дня и с
    запасом в полдень, чтобы гарантированно попасть в окно
    [start_of_day_utc, следующий день) независимо от MSK/UTC-сдвига."""
    noon_msk = datetime.combine(day, time(12, 0), tzinfo=quests_service.MSK)
    return noon_msk.astimezone(quests_service.UTC).replace(tzinfo=None)


async def _seed_economy_tx(
    session, chat_id: int, user_id: int, kind: str, day: date, amount: int = 1
) -> None:
    session.add(
        EconomyTx(
            chat_id=chat_id,
            user_id=user_id,
            amount=amount,
            kind=kind,
            created_at=_msk_noon_utc_naive(day),
        )
    )
    await session.flush()


def _patch_today(monkeypatch, day: date) -> None:
    monkeypatch.setattr(quests_service, "_today_msk", lambda: day)


async def _completion_count(session, chat_id: int, user_id: int, quest_key: str) -> int:
    return (
        await session.execute(
            select(func.count())
            .select_from(QuestCompletion)
            .where(
                QuestCompletion.chat_id == chat_id,
                QuestCompletion.user_id == user_id,
                QuestCompletion.quest_key == quest_key,
            )
        )
    ).scalar_one()


async def _get_completion(
    session, chat_id: int, user_id: int, quest_key: str, day: date
) -> QuestCompletion | None:
    return (
        await session.execute(
            select(QuestCompletion).where(
                QuestCompletion.chat_id == chat_id,
                QuestCompletion.user_id == user_id,
                QuestCompletion.quest_key == quest_key,
                QuestCompletion.day_msk == day,
            )
        )
    ).scalar_one_or_none()


async def _economy_tx_rows(session, chat_id: int, user_id: int, kind: str) -> list[EconomyTx]:
    return list(
        (
            await session.execute(
                select(EconomyTx).where(
                    EconomyTx.chat_id == chat_id,
                    EconomyTx.user_id == user_id,
                    EconomyTx.kind == kind,
                )
            )
        )
        .scalars()
        .all()
    )


def _claimed_keys(result: dict) -> set[str]:
    return {c["key"] for c in result["claimed"]}


# --- 1. Каждый из 6 квестов по отдельности ------------------------------------


@pytest.mark.asyncio
async def test_quest_active_claim_pays_reward(session, monkeypatch):
    _patch_today(monkeypatch, TEST_DAY)
    chat_id, user_id = -100910301, 910301
    await _ensure_user(session, user_id)
    await _seed_daily_stat(session, chat_id, user_id, TEST_DAY, message_count=10)

    result = await quests_service.claim_quests(session, chat_id, user_id)

    assert "active" in _claimed_keys(result)
    completion = await _get_completion(session, chat_id, user_id, "active", TEST_DAY)
    assert completion is not None
    assert completion.reward_amount == _REWARDS["active"]

    tx_rows = await _economy_tx_rows(session, chat_id, user_id, "quest_active")
    assert len(tx_rows) == 1
    assert tx_rows[0].amount == _REWARDS["active"]


@pytest.mark.asyncio
async def test_quest_photo_claim_pays_reward(session, monkeypatch):
    _patch_today(monkeypatch, TEST_DAY)
    chat_id, user_id = -100910302, 910302
    await _ensure_user(session, user_id)
    await _seed_daily_stat(session, chat_id, user_id, TEST_DAY, photo_count=1)

    result = await quests_service.claim_quests(session, chat_id, user_id)

    assert "photo" in _claimed_keys(result)
    completion = await _get_completion(session, chat_id, user_id, "photo", TEST_DAY)
    assert completion is not None
    assert completion.reward_amount == _REWARDS["photo"]

    tx_rows = await _economy_tx_rows(session, chat_id, user_id, "quest_photo")
    assert len(tx_rows) == 1
    assert tx_rows[0].amount == _REWARDS["photo"]


@pytest.mark.asyncio
async def test_quest_social_claim_pays_reward(session, monkeypatch):
    _patch_today(monkeypatch, TEST_DAY)
    chat_id, user_id = -100910303, 910303
    await _ensure_user(session, user_id)
    await _seed_economy_tx(session, chat_id, user_id, "social_poke", TEST_DAY)

    result = await quests_service.claim_quests(session, chat_id, user_id)

    assert "social" in _claimed_keys(result)
    completion = await _get_completion(session, chat_id, user_id, "social", TEST_DAY)
    assert completion is not None
    assert completion.reward_amount == _REWARDS["social"]

    tx_rows = await _economy_tx_rows(session, chat_id, user_id, "quest_social")
    assert len(tx_rows) == 1
    assert tx_rows[0].amount == _REWARDS["social"]


@pytest.mark.asyncio
async def test_quest_media_claim_pays_reward(session, monkeypatch):
    _patch_today(monkeypatch, TEST_DAY)
    chat_id, user_id = -100910304, 910304
    await _ensure_user(session, user_id)
    await _seed_economy_tx(session, chat_id, user_id, "mediadl_charge", TEST_DAY)

    result = await quests_service.claim_quests(session, chat_id, user_id)

    assert "media" in _claimed_keys(result)
    completion = await _get_completion(session, chat_id, user_id, "media", TEST_DAY)
    assert completion is not None
    assert completion.reward_amount == _REWARDS["media"]

    tx_rows = await _economy_tx_rows(session, chat_id, user_id, "quest_media")
    assert len(tx_rows) == 1
    assert tx_rows[0].amount == _REWARDS["media"]


@pytest.mark.asyncio
async def test_quest_duel_claim_pays_reward(session, monkeypatch):
    _patch_today(monkeypatch, TEST_DAY)
    chat_id, user_id = -100910305, 910305
    await _ensure_user(session, user_id)
    await _seed_economy_tx(session, chat_id, user_id, "duel_stake", TEST_DAY)

    result = await quests_service.claim_quests(session, chat_id, user_id)

    assert "duel" in _claimed_keys(result)
    completion = await _get_completion(session, chat_id, user_id, "duel", TEST_DAY)
    assert completion is not None
    assert completion.reward_amount == _REWARDS["duel"]

    tx_rows = await _economy_tx_rows(session, chat_id, user_id, "quest_duel")
    assert len(tx_rows) == 1
    assert tx_rows[0].amount == _REWARDS["duel"]


@pytest.mark.asyncio
async def test_quest_gacha_claim_pays_reward(session, monkeypatch):
    _patch_today(monkeypatch, TEST_DAY)
    chat_id, user_id = -100910306, 910306
    await _ensure_user(session, user_id)
    await _seed_economy_tx(session, chat_id, user_id, "gacha_roll", TEST_DAY)

    result = await quests_service.claim_quests(session, chat_id, user_id)

    assert "gacha" in _claimed_keys(result)
    completion = await _get_completion(session, chat_id, user_id, "gacha", TEST_DAY)
    assert completion is not None
    assert completion.reward_amount == _REWARDS["gacha"]

    tx_rows = await _economy_tx_rows(session, chat_id, user_id, "quest_gacha")
    assert len(tx_rows) == 1
    assert tx_rows[0].amount == _REWARDS["gacha"]


# --- 2. Идемпотентность в рамках одного MSK-дня -------------------------------


@pytest.mark.asyncio
async def test_claim_quests_idempotent_same_day_no_double_payout(session, monkeypatch):
    _patch_today(monkeypatch, TEST_DAY)
    chat_id, user_id = -100910307, 910307
    await _ensure_user(session, user_id)
    await _seed_daily_stat(session, chat_id, user_id, TEST_DAY, message_count=10)

    first = await quests_service.claim_quests(session, chat_id, user_id)
    assert "active" in _claimed_keys(first)

    # Условие всё ещё истинно (та же строка daily_stats на тот же день) —
    # повторный вызов НЕ должен заплатить ещё раз.
    second = await quests_service.claim_quests(session, chat_id, user_id)
    assert "active" not in _claimed_keys(second)

    assert await _completion_count(session, chat_id, user_id, "active") == 1
    tx_rows = await _economy_tx_rows(session, chat_id, user_id, "quest_active")
    assert len(tx_rows) == 1


# --- 3. Смена MSK-дня: доступность квеста сбрасывается -----------------------


@pytest.mark.asyncio
async def test_claim_quests_reclaims_after_day_rollover(session, monkeypatch):
    chat_id, user_id = -100910308, 910308
    await _ensure_user(session, user_id)

    _patch_today(monkeypatch, TEST_DAY)
    await _seed_daily_stat(session, chat_id, user_id, TEST_DAY, message_count=10)
    day1 = await quests_service.claim_quests(session, chat_id, user_id)
    assert "active" in _claimed_keys(day1)

    _patch_today(monkeypatch, DAY_2)
    await _seed_daily_stat(session, chat_id, user_id, DAY_2, message_count=10)
    day2 = await quests_service.claim_quests(session, chat_id, user_id)
    assert "active" in _claimed_keys(day2)

    assert await _get_completion(session, chat_id, user_id, "active", TEST_DAY) is not None
    assert await _get_completion(session, chat_id, user_id, "active", DAY_2) is not None
    assert await _completion_count(session, chat_id, user_id, "active") == 2

    tx_rows = await _economy_tx_rows(session, chat_id, user_id, "quest_active")
    assert len(tx_rows) == 2


# --- 4. get_total_completions: кумулятивный счётчик ---------------------------


@pytest.mark.asyncio
async def test_get_total_completions_across_keys_and_days(session, monkeypatch):
    chat_id, user_id = -100910309, 910309
    await _ensure_user(session, user_id)

    _patch_today(monkeypatch, TEST_DAY)
    await _seed_daily_stat(
        session, chat_id, user_id, TEST_DAY, message_count=10, photo_count=1
    )
    day1 = await quests_service.claim_quests(session, chat_id, user_id)
    assert _claimed_keys(day1) == {"active", "photo"}

    _patch_today(monkeypatch, DAY_2)
    await _seed_daily_stat(session, chat_id, user_id, DAY_2, message_count=10)
    day2 = await quests_service.claim_quests(session, chat_id, user_id)
    assert _claimed_keys(day2) == {"active"}

    total = await quests_service.get_total_completions(session, chat_id, user_id)
    assert total == 3


# --- 5. get_quest_status: done/claimed до и после claim -----------------------


@pytest.mark.asyncio
async def test_get_quest_status_reflects_done_then_claimed(session, monkeypatch):
    _patch_today(monkeypatch, TEST_DAY)
    chat_id, user_id = -100910310, 910310
    await _ensure_user(session, user_id)
    await _seed_economy_tx(session, chat_id, user_id, "gacha_roll", TEST_DAY)

    status_before = await quests_service.get_quest_status(session, chat_id, user_id)
    gacha_before = next(q for q in status_before if q["key"] == "gacha")
    assert gacha_before["done"] is True
    assert gacha_before["claimed"] is False

    other_before = [q for q in status_before if q["key"] != "gacha"]
    assert all(q["done"] is False for q in other_before)
    assert all(q["claimed"] is False for q in status_before)

    await quests_service.claim_quests(session, chat_id, user_id)

    status_after = await quests_service.get_quest_status(session, chat_id, user_id)
    gacha_after = next(q for q in status_after if q["key"] == "gacha")
    assert gacha_after["done"] is True
    assert gacha_after["claimed"] is True
