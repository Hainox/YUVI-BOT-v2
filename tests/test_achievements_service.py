"""Интеграционные тесты achievements_service (QUEST-02) против живого Postgres
(фикстура `session` из tests/conftest.py — транзакция-на-тест, тот же паттерн,
что tests/test_clicker_service.py: локальные seed-хелперы, без моков).

Что проверяем:
- Каждая из 6 ачивок реально разблокируется РОВНО от своего условия (и только
  своего — `_claim_and_assert_unlocked` требует, чтобы `unlocked` в ответе
  `claim_achievements` содержал ровно один ключ, не больше) и платит ровно
  один раз (`economy_tx.kind = f"achievement_{key}"`).
- One-time-forever: `uq_achievement_unlock` (chat_id, user_id, achievement_key)
  без даты — повторный `claim_achievements` с тем же (тривиально всё ещё
  истинным) условием не создаёт вторую строку `achievement_unlocks` и не
  платит повторно.
- `get_total_bonus_levels` суммирует `bonus_levels` по нескольким
  разблокированным ачивкам одного участника.
- `get_achievement_status` не путает "условие выполнено" с "ачивка реально
  выдана и оплачена" — `unlocked=True` появляется только ПОСЛЕ
  `claim_achievements`, а не в момент, когда условие стало истинным.
"""

from __future__ import annotations

from datetime import date
from datetime import timedelta

import pytest
from sqlalchemy import func
from sqlalchemy import select

from bot.services import achievements_service
from common.models.achievement_unlock import AchievementUnlock
from common.models.daily_stat import DailyStat
from common.models.duel import Duel
from common.models.economy_tx import EconomyTx
from common.models.gacha_collection import GachaCollection
from common.models.quest_completion import QuestCompletion
from common.models.user import User


# --- seed-хелперы: минимальные данные под ровно одно условие -----------------


async def _ensure_user(session, user_id: int, first_name: str = "Тест") -> None:
    session.add(User(id=user_id, first_name=first_name))
    await session.flush()


async def _seed_streak_days(session, chat_id: int, user_id: int, num_days: int) -> None:
    """`num_days` подряд идущих дней активности (message_count>0), без
    привязки к "сегодня" — get_streak меряет серию от самой поздней даты
    назад, поэтому подойдёт любой непрерывный интервал."""
    base = date.today()
    for i in range(num_days):
        session.add(
            DailyStat(
                chat_id=chat_id,
                user_id=user_id,
                stat_date=base - timedelta(days=i),
                message_count=1,
            )
        )
    await session.flush()


async def _seed_total_messages(session, chat_id: int, user_id: int, total: int) -> None:
    """Одна строка daily_stats с суммой сообщений >= порога — намеренно
    ОДИН день (streak=1), чтобы не задеть условие streak7 попутно."""
    session.add(
        DailyStat(chat_id=chat_id, user_id=user_id, stat_date=date.today(), message_count=total)
    )
    await session.flush()


async def _seed_resolved_duels_won(session, chat_id: int, user_id: int, count: int) -> None:
    for _ in range(count):
        session.add(
            Duel(
                chat_id=chat_id,
                challenger_id=user_id,
                winner_id=user_id,
                stake=10,
                status="resolved",
            )
        )
    await session.flush()


async def _seed_gacha_collection(session, chat_id: int, user_id: int, count: int) -> None:
    for i in range(count):
        session.add(
            GachaCollection(
                chat_id=chat_id, user_id=user_id, char_id=f"char_{i}", stars=1, copies=1
            )
        )
    await session.flush()


async def _seed_media_charges(session, chat_id: int, user_id: int, count: int) -> None:
    for _ in range(count):
        session.add(
            EconomyTx(chat_id=chat_id, user_id=user_id, amount=-5, kind="mediadl_charge")
        )
    await session.flush()


async def _seed_quest_completions(session, chat_id: int, user_id: int, count: int) -> None:
    """Прямой сид quest_completions (в обход реального claim-флоу квестов —
    для этого условия он не нужен): UNIQUE(chat_id, user_id, quest_key,
    day_msk), поэтому `count` разных day_msk на один и тот же quest_key
    дают `count` валидных строк."""
    base = date(2020, 1, 1)
    for i in range(count):
        session.add(
            QuestCompletion(
                chat_id=chat_id,
                user_id=user_id,
                quest_key="active",
                day_msk=base + timedelta(days=i),
                reward_amount=10,
            )
        )
    await session.flush()


# --- read-хелперы --------------------------------------------------------------


def _catalog(key: str) -> achievements_service.AchievementDef:
    return next(a for a in achievements_service.ACHIEVEMENTS if a.key == key)


async def _get_unlock(session, chat_id: int, user_id: int, key: str) -> AchievementUnlock | None:
    return (
        await session.execute(
            select(AchievementUnlock).where(
                AchievementUnlock.chat_id == chat_id,
                AchievementUnlock.user_id == user_id,
                AchievementUnlock.achievement_key == key,
            )
        )
    ).scalar_one_or_none()


async def _tx_amounts(session, chat_id: int, user_id: int, kind: str) -> list[int]:
    return list(
        (
            await session.execute(
                select(EconomyTx.amount).where(
                    EconomyTx.chat_id == chat_id,
                    EconomyTx.user_id == user_id,
                    EconomyTx.kind == kind,
                )
            )
        )
        .scalars()
        .all()
    )


async def _claim_and_assert_unlocked(session, chat_id: int, user_id: int, key: str) -> None:
    """Общая часть проверки для каждой из 6 ачивок по отдельности: claim
    разблокировал РОВНО этот ключ (не больше — доказывает, что seed минимален
    и не задевает другие условия попутно), строка achievement_unlocks несёт
    правильный снапшот каталога, и выплата ушла ровно одним economy_tx."""
    catalog = _catalog(key)

    result = await achievements_service.claim_achievements(session, chat_id, user_id)
    assert {u["key"] for u in result["unlocked"]} == {key}

    unlock = await _get_unlock(session, chat_id, user_id, key)
    assert unlock is not None
    assert unlock.bonus_levels == catalog.bonus_levels
    assert unlock.reward_amount == catalog.reward

    amounts = await _tx_amounts(session, chat_id, user_id, f"achievement_{key}")
    assert amounts == [catalog.reward]


# --- по одной ачивке на условие -------------------------------------------------


@pytest.mark.asyncio
async def test_claim_streak7_unlocks_from_seven_consecutive_days(session):
    chat_id = -100910100
    user_id = 910100
    await _ensure_user(session, user_id)
    await _seed_streak_days(session, chat_id, user_id, 7)

    await _claim_and_assert_unlocked(session, chat_id, user_id, "streak7")


@pytest.mark.asyncio
async def test_claim_messages1000_unlocks_from_summed_message_count(session):
    chat_id = -100910101
    user_id = 910101
    await _ensure_user(session, user_id)
    await _seed_total_messages(session, chat_id, user_id, 1000)

    await _claim_and_assert_unlocked(session, chat_id, user_id, "messages1000")


@pytest.mark.asyncio
async def test_claim_duels10_unlocks_from_ten_resolved_wins(session):
    chat_id = -100910102
    user_id = 910102
    await _ensure_user(session, user_id)
    await _seed_resolved_duels_won(session, chat_id, user_id, 10)

    await _claim_and_assert_unlocked(session, chat_id, user_id, "duels10")


@pytest.mark.asyncio
async def test_claim_gacha10_unlocks_from_ten_distinct_characters(session):
    chat_id = -100910103
    user_id = 910103
    await _ensure_user(session, user_id)
    await _seed_gacha_collection(session, chat_id, user_id, 10)

    await _claim_and_assert_unlocked(session, chat_id, user_id, "gacha10")


@pytest.mark.asyncio
async def test_claim_media20_unlocks_from_twenty_media_charges(session):
    chat_id = -100910104
    user_id = 910104
    await _ensure_user(session, user_id)
    await _seed_media_charges(session, chat_id, user_id, 20)

    await _claim_and_assert_unlocked(session, chat_id, user_id, "media20")


@pytest.mark.asyncio
async def test_claim_quests50_unlocks_from_fifty_quest_completions(session):
    chat_id = -100910105
    user_id = 910105
    await _ensure_user(session, user_id)
    await _seed_quest_completions(session, chat_id, user_id, 50)

    await _claim_and_assert_unlocked(session, chat_id, user_id, "quests50")


# --- one-time-forever ------------------------------------------------------------


@pytest.mark.asyncio
async def test_claim_achievement_is_one_time_forever(session):
    """Условие остаётся тривиально истинным (данные никуда не делись), но
    повторный claim не должен ни создать вторую строку achievement_unlocks,
    ни выплатить награду повторно — `on_conflict_do_nothing` на
    (chat_id, user_id, achievement_key) единственный арбитр."""
    chat_id = -100910106
    user_id = 910106
    await _ensure_user(session, user_id)
    await _seed_gacha_collection(session, chat_id, user_id, 10)

    first = await achievements_service.claim_achievements(session, chat_id, user_id)
    assert {u["key"] for u in first["unlocked"]} == {"gacha10"}

    second = await achievements_service.claim_achievements(session, chat_id, user_id)
    assert second["unlocked"] == []
    assert second["total_reward"] == 0

    unlock_count = (
        await session.execute(
            select(func.count())
            .select_from(AchievementUnlock)
            .where(
                AchievementUnlock.chat_id == chat_id,
                AchievementUnlock.user_id == user_id,
                AchievementUnlock.achievement_key == "gacha10",
            )
        )
    ).scalar_one()
    assert unlock_count == 1

    amounts = await _tx_amounts(session, chat_id, user_id, "achievement_gacha10")
    assert amounts == [_catalog("gacha10").reward]


# --- get_total_bonus_levels -------------------------------------------------------


@pytest.mark.asyncio
async def test_get_total_bonus_levels_sums_multiple_unlocked_achievements(session):
    chat_id = -100910107
    user_id = 910107
    await _ensure_user(session, user_id)

    # Одновременно удовлетворяем 3 разных условия: messages1000 (+3),
    # duels10 (+3), quests50 (+5) — один claim_achievements разблокирует
    # все три сразу.
    await _seed_total_messages(session, chat_id, user_id, 1000)
    await _seed_resolved_duels_won(session, chat_id, user_id, 10)
    await _seed_quest_completions(session, chat_id, user_id, 50)

    result = await achievements_service.claim_achievements(session, chat_id, user_id)
    assert {u["key"] for u in result["unlocked"]} == {"messages1000", "duels10", "quests50"}

    expected_sum = sum(
        _catalog(key).bonus_levels for key in ("messages1000", "duels10", "quests50")
    )
    total = await achievements_service.get_total_bonus_levels(session, chat_id, user_id)
    assert total == expected_sum


# --- get_achievement_status: unlocked != условие выполнено ------------------------


@pytest.mark.asyncio
async def test_achievement_status_unlocked_only_after_claim(session):
    chat_id = -100910108
    user_id = 910108
    await _ensure_user(session, user_id)

    status_before = await achievements_service.get_achievement_status(session, chat_id, user_id)
    assert next(s for s in status_before if s["key"] == "media20")["unlocked"] is False

    # Условие выполнено, но claim_achievements ещё не вызывался — статус
    # ДОЛЖЕН оставаться unlocked=False (иначе "eligible" и "выдано/оплачено"
    # смешались бы в один и тот же флаг).
    await _seed_media_charges(session, chat_id, user_id, 20)
    status_condition_met = await achievements_service.get_achievement_status(
        session, chat_id, user_id
    )
    assert next(s for s in status_condition_met if s["key"] == "media20")["unlocked"] is False

    await achievements_service.claim_achievements(session, chat_id, user_id)

    status_after_claim = await achievements_service.get_achievement_status(
        session, chat_id, user_id
    )
    assert next(s for s in status_after_claim if s["key"] == "media20")["unlocked"] is True
