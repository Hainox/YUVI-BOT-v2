"""Тесты bot/services/admin_analytics_service.py (CASINO-03, D-03) — против
живого Postgres через фикстуру `session` из tests/conftest.py (транзакция-на-
тест, join-savepoint режим — тот же паттерн, что test_economy_service.py/
test_feedback_service.py).

RED (Task 1): `bot/services/admin_analytics_service.py` ещё не существует —
`from bot.services import admin_analytics_service` падает ImportError.
Реализация — Task 2 (GREEN).

Знаковая конвенция EconomyTx (04.3-RESEARCH.md Pitfall 2): casino_bet
user-side нога отрицательная (списание), bank-side (user_id IS NULL)
положительная; casino_payout — зеркально (user-side положительная, bank-side
отрицательная). get_turnover должен корректно разделять эти ноги.
"""

from __future__ import annotations

from datetime import datetime
from datetime import timedelta

import pytest

from bot.services import admin_analytics_service
from common.models.casino_game import CasinoGame
from common.models.economy_tx import EconomyTx
from common.models.user import User

CHAT_A = -900501
CHAT_B = -900502


async def _ensure_user(session, user_id: int, first_name: str = "Тест") -> None:
    session.add(User(id=user_id, first_name=first_name))
    await session.flush()


async def _insert_casino_game(
    session, chat_id: int, user_id: int, game: str, created_at: datetime
) -> None:
    session.add(
        CasinoGame(
            chat_id=chat_id,
            user_id=user_id,
            game=game,
            bet=100,
            payout=0,
            status="settled",
            created_at=created_at,
        )
    )
    await session.flush()


async def _insert_tx(
    session,
    chat_id: int,
    user_id: int | None,
    amount: int,
    kind: str,
    ref_id: str,
    created_at: datetime,
) -> None:
    session.add(
        EconomyTx(
            chat_id=chat_id,
            user_id=user_id,
            amount=amount,
            kind=kind,
            ref_id=ref_id,
            created_at=created_at,
        )
    )
    await session.flush()


# --- get_game_popularity -----------------------------------------------------


@pytest.mark.asyncio
async def test_game_popularity_counts_and_orders_desc(session):
    user_id = 340101
    await _ensure_user(session, user_id)
    now = datetime.utcnow()
    since = now - timedelta(days=1)

    for _ in range(3):
        await _insert_casino_game(session, CHAT_A, user_id, "slots", now)
    await _insert_casino_game(session, CHAT_A, user_id, "dice", now)

    result = await admin_analytics_service.get_game_popularity(session, CHAT_A, since)

    assert result[0] == {"game": "slots", "rounds": 3}
    assert result[1] == {"game": "dice", "rounds": 1}


@pytest.mark.asyncio
async def test_game_popularity_scoped_by_chat_and_since(session):
    user_id = 340102
    await _ensure_user(session, user_id)
    now = datetime.utcnow()
    since = now - timedelta(hours=1)
    too_old = now - timedelta(days=2)

    await _insert_casino_game(session, CHAT_A, user_id, "roulette", now)
    await _insert_casino_game(session, CHAT_B, user_id, "roulette", now)  # другой чат
    await _insert_casino_game(session, CHAT_A, user_id, "roulette", too_old)  # до since

    result = await admin_analytics_service.get_game_popularity(session, CHAT_A, since)

    assert result == [{"game": "roulette", "rounds": 1}]


# --- get_turnover -------------------------------------------------------------


@pytest.mark.asyncio
async def test_turnover_matches_known_bet_payout_fixture(session):
    """Ставка 100, выплата 80 (комиссия банка 20) — Pitfall 2 знаковая
    конвенция: casino_bet user-side отрицательный/bank-side положительный,
    casino_payout — зеркально."""
    user_id = 340103
    await _ensure_user(session, user_id)
    now = datetime.utcnow()
    since = now - timedelta(days=1)

    await _insert_tx(session, CHAT_A, user_id, -100, "casino_bet", "t1:user", now)
    await _insert_tx(session, CHAT_A, None, 100, "casino_bet", "t1:bank", now)
    await _insert_tx(session, CHAT_A, user_id, 80, "casino_payout", "t1:user:payout", now)
    await _insert_tx(session, CHAT_A, None, -80, "casino_payout", "t1:bank:payout", now)

    result = await admin_analytics_service.get_turnover(session, CHAT_A, since)

    assert result["bets_placed"] == 100
    assert result["bank_commission"] == 20


@pytest.mark.asyncio
async def test_turnover_scoped_by_chat_and_since(session):
    user_id = 340104
    await _ensure_user(session, user_id)
    now = datetime.utcnow()
    since = now - timedelta(hours=1)
    too_old = now - timedelta(days=2)

    await _insert_tx(session, CHAT_A, user_id, -50, "casino_bet", "t2:user", now)
    await _insert_tx(session, CHAT_A, None, 50, "casino_bet", "t2:bank", now)
    await _insert_tx(session, CHAT_B, user_id, -999, "casino_bet", "t2:other_chat", now)
    await _insert_tx(session, CHAT_A, user_id, -999, "casino_bet", "t2:too_old", too_old)

    result = await admin_analytics_service.get_turnover(session, CHAT_A, since)

    assert result["bets_placed"] == 50
    assert result["bank_commission"] == 50


# --- get_active_players -------------------------------------------------------


@pytest.mark.asyncio
async def test_active_players_counts_distinct_per_day(session):
    user_a, user_b, user_c = 340105, 340106, 340107
    await _ensure_user(session, user_a)
    await _ensure_user(session, user_b)
    await _ensure_user(session, user_c)

    day1 = datetime(2026, 6, 1, 12, 0, 0)
    day2 = datetime(2026, 6, 2, 12, 0, 0)
    since = day1 - timedelta(hours=1)

    await _insert_casino_game(session, CHAT_A, user_a, "dice", day1)
    await _insert_casino_game(session, CHAT_A, user_b, "dice", day1)
    await _insert_casino_game(session, CHAT_A, user_a, "dice", day2)  # same user, different day
    await _insert_casino_game(session, CHAT_A, user_c, "dice", day2)

    result = await admin_analytics_service.get_active_players(session, CHAT_A, since)

    by_day = {row["day"]: row["active_players"] for row in result}
    assert by_day[str(day1.date())] == 2
    assert by_day[str(day2.date())] == 2


@pytest.mark.asyncio
async def test_active_players_scoped_by_chat(session):
    user_id = 340108
    await _ensure_user(session, user_id)
    day1 = datetime(2026, 6, 3, 12, 0, 0)
    since = day1 - timedelta(hours=1)

    await _insert_casino_game(session, CHAT_B, user_id, "dice", day1)  # другой чат

    result = await admin_analytics_service.get_active_players(session, CHAT_A, since)

    assert result == []
