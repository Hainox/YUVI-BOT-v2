"""Интеграционные тесты awards_service (AWARDS-01/AWARDS-02) против живого
Postgres (фикстура `session` из tests/conftest.py — форма test_duel_service.py:
_ensure_user/_fund/_get_bank_balance/_get_user_balance).

Доказывает:
- compute_nominations выдаёт корректных победителей по всем 6 метрикам
  daily_stats (max для 5 DESC-номинаций, min для least_active) — читает
  ГОТОВЫЙ агрегат daily_stats, никакого COUNT(*) по messages (RESEARCH.md
  Anti-Pattern).
- Детерминированный тай-брейк (user_id ASC), не рандом (least_active).
- run_awards идемпотентен по ref_id=f"award:{chat_id}:{day_msk}:{key}"
  (AWARDS-02/D-09): повторный запуск в тот же MSK-день не платит повторно.
- Выплаты реально идут из банка чата (economy_service.pay_from_bank).
- Номинация с "пустой" метрикой (все 0) пропускается без выплаты — не
  "никто" получает 228 ювиков просто за отсутствие мата у всех.

awards_service._today_msk() — собственный monkeypatchable seam (форма
daily_pick_service._today_msk), НЕ daily_pick_service — awards
самодостаточен (05-06-PLAN.md must_haves).
"""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import select

from bot.services import awards_service
from bot.services import economy_service
from common.models.chat_bank import ChatBank
from common.models.daily_stat import DailyStat
from common.models.user import User
from common.models.user_balance import UserBalance

TEST_DAY = date(2026, 7, 19)


# --- Хелперы (форма test_duel_service.py) ------------------------------------


async def _ensure_user(session, user_id: int, first_name: str = "Тест") -> None:
    session.add(User(id=user_id, first_name=first_name))
    await session.flush()


async def _fund(session, chat_id: int, user_id: int) -> int:
    """Заводит кошелёк (стартовый бонус) и коммитит."""
    return await economy_service.get_balance(session, chat_id, user_id)


async def _get_user_balance(session, chat_id: int, user_id: int) -> int:
    result = await session.execute(
        select(UserBalance.balance).where(
            UserBalance.chat_id == chat_id, UserBalance.user_id == user_id
        )
    )
    return result.scalar_one()


async def _get_bank_balance(session, chat_id: int) -> int:
    result = await session.execute(select(ChatBank.balance).where(ChatBank.chat_id == chat_id))
    return result.scalar_one_or_none() or 0


async def _seed_daily_stat(
    session,
    chat_id: int,
    user_id: int,
    stat_date: date,
    *,
    message_count: int = 0,
    profanity_count: int = 0,
    photo_count: int = 0,
    forward_count: int = 0,
    longest_msg_len: int = 0,
) -> None:
    session.add(
        DailyStat(
            chat_id=chat_id,
            user_id=user_id,
            stat_date=stat_date,
            message_count=message_count,
            profanity_count=profanity_count,
            photo_count=photo_count,
            forward_count=forward_count,
            longest_msg_len=longest_msg_len,
        )
    )
    await session.flush()


def _patch_today(monkeypatch, day: date = TEST_DAY) -> None:
    monkeypatch.setattr(awards_service, "_today_msk", lambda: day)


def _nom(noms: list[dict], key: str) -> dict:
    for nom in noms:
        if nom["key"] == key:
            return nom
    raise AssertionError(f"nomination not found: {key}")


# --- compute_nominations: скоринг по всем 6 метрикам -------------------------


@pytest.mark.asyncio
async def test_nomination_scoring(session):
    chat_id = -100900660001
    user_a, user_b, user_c = 900660001, 900660002, 900660003
    await _ensure_user(session, user_a, "Альфа")
    await _ensure_user(session, user_b, "Бета")
    await _ensure_user(session, user_c, "Гамма")

    await _seed_daily_stat(
        session,
        chat_id,
        user_a,
        TEST_DAY,
        message_count=50,
        profanity_count=10,
        photo_count=5,
        forward_count=3,
        longest_msg_len=400,
    )
    await _seed_daily_stat(
        session,
        chat_id,
        user_b,
        TEST_DAY,
        message_count=20,
        profanity_count=2,
        photo_count=1,
        forward_count=1,
        longest_msg_len=100,
    )
    await _seed_daily_stat(
        session,
        chat_id,
        user_c,
        TEST_DAY,
        message_count=1,
        profanity_count=0,
        photo_count=0,
        forward_count=0,
        longest_msg_len=10,
    )

    noms = await awards_service.compute_nominations(session, chat_id, TEST_DAY)

    assert _nom(noms, "most_messages")["winner_user_id"] == user_a
    assert _nom(noms, "most_messages")["metric_value"] == 50
    assert _nom(noms, "profanity")["winner_user_id"] == user_a
    assert _nom(noms, "photos")["winner_user_id"] == user_a
    assert _nom(noms, "forwards")["winner_user_id"] == user_a
    assert _nom(noms, "longest")["winner_user_id"] == user_a
    assert _nom(noms, "least_active")["winner_user_id"] == user_c
    assert _nom(noms, "least_active")["metric_value"] == 1


@pytest.mark.asyncio
async def test_least_active_deterministic_tiebreak(session):
    """Равенство message_count у нескольких участников — победитель
    детерминирован (user_id ASC), не рандом."""
    chat_id = -100900660004
    lower_id, higher_id = 900660010, 900660020
    await _ensure_user(session, higher_id, "Второй")
    await _ensure_user(session, lower_id, "Первый")

    await _seed_daily_stat(session, chat_id, higher_id, TEST_DAY, message_count=5)
    await _seed_daily_stat(session, chat_id, lower_id, TEST_DAY, message_count=5)

    noms = await awards_service.compute_nominations(session, chat_id, TEST_DAY)

    assert _nom(noms, "least_active")["winner_user_id"] == lower_id

    # Повторный вызов — тот же результат (детерминизм, не рандом).
    noms_again = await awards_service.compute_nominations(session, chat_id, TEST_DAY)
    assert _nom(noms_again, "least_active")["winner_user_id"] == lower_id


@pytest.mark.asyncio
async def test_empty_metric_nomination_skipped(session):
    """Если profanity_count у всех 0 — номинация «матершинник» без победителя
    (никто), а не случайный участник с нулевым значением."""
    chat_id = -100900660005
    user_id = 900660030
    await _ensure_user(session, user_id, "Чистюля")
    await _seed_daily_stat(
        session, chat_id, user_id, TEST_DAY, message_count=10, profanity_count=0
    )

    noms = await awards_service.compute_nominations(session, chat_id, TEST_DAY)

    assert _nom(noms, "profanity")["winner_user_id"] is None


# --- run_awards: идемпотентные выплаты из банка (AWARDS-02) ------------------


@pytest.mark.asyncio
async def test_awards_payout_from_bank(session, monkeypatch):
    _patch_today(monkeypatch)
    chat_id = -100900660006
    user_a, user_b = 900660040, 900660041
    await _ensure_user(session, user_a, "Главный")
    await _ensure_user(session, user_b, "Тихий")
    balance_a_before = await _fund(session, chat_id, user_a)
    balance_b_before = await _fund(session, chat_id, user_b)
    await economy_service.credit_bank(
        session, chat_id, 100_000, kind="test_seed", ref_id="test_awards_payout_seed"
    )
    await session.commit()

    await _seed_daily_stat(
        session,
        chat_id,
        user_a,
        TEST_DAY,
        message_count=50,
        profanity_count=10,
        photo_count=5,
        forward_count=3,
        longest_msg_len=400,
    )
    await _seed_daily_stat(session, chat_id, user_b, TEST_DAY, message_count=1)

    await awards_service.run_awards(session, chat_id)

    # user_a выигрывает main(322) + profanity/photos/forwards/longest(228*4)
    assert (
        await _get_user_balance(session, chat_id, user_a)
        == balance_a_before + 322 + 228 * 4
    )
    # user_b выигрывает least_active(228)
    assert await _get_user_balance(session, chat_id, user_b) == balance_b_before + 228


@pytest.mark.asyncio
async def test_awards_idempotent_on_replay(session, monkeypatch):
    """Повторный run_awards в тот же MSK-день не платит повторно —
    ref_id=f"award:{chat_id}:{day_msk}:{key}" ловит IntegrityError, деньги не
    двигаются второй раз."""
    _patch_today(monkeypatch)
    chat_id = -100900660007
    user_a, user_b = 900660050, 900660051
    await _ensure_user(session, user_a, "Главный")
    await _ensure_user(session, user_b, "Тихий")
    await _fund(session, chat_id, user_a)
    await _fund(session, chat_id, user_b)
    await economy_service.credit_bank(
        session, chat_id, 100_000, kind="test_seed", ref_id="test_awards_idem_seed"
    )
    await session.commit()

    await _seed_daily_stat(
        session,
        chat_id,
        user_a,
        TEST_DAY,
        message_count=50,
        profanity_count=10,
        photo_count=5,
        forward_count=3,
        longest_msg_len=400,
    )
    await _seed_daily_stat(session, chat_id, user_b, TEST_DAY, message_count=1)
    bank_before = await _get_bank_balance(session, chat_id)

    first = await awards_service.run_awards(session, chat_id)
    balance_a_after_first = await _get_user_balance(session, chat_id, user_a)
    balance_b_after_first = await _get_user_balance(session, chat_id, user_b)
    bank_after_first = await _get_bank_balance(session, chat_id)

    second = await awards_service.run_awards(session, chat_id)

    assert await _get_user_balance(session, chat_id, user_a) == balance_a_after_first
    assert await _get_user_balance(session, chat_id, user_b) == balance_b_after_first
    assert await _get_bank_balance(session, chat_id) == bank_after_first
    assert bank_after_first == bank_before - (322 + 228 * 4 + 228)

    winners_first = {n["key"]: n["winner_user_id"] for n in first["nominations"]}
    winners_second = {n["key"]: n["winner_user_id"] for n in second["nominations"]}
    assert winners_first == winners_second
