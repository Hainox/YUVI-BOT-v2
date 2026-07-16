"""Интеграционные тесты economy_service против живого Postgres (фикстура
`session` из tests/conftest.py — транзакция-на-тест). Доказывают ECON-01/03:
стартовый бонус начисляется ровно один раз, комиссия перевода 5% (min 1)
уходит в банк (D-04), повтор ref_id не двоит деньги (идемпотентность через
частичный UNIQUE(chat_id, ref_id, kind) + SAVEPOINT), economy_tx — append-only.

Сервис сам делает session.commit() там, где это описано в его контракте
(get_balance, transfer_with_fee) — совместимо с фикстурой session благодаря
join-savepoint режиму SQLAlchemy 2.0 (см. test_backfill_idempotency.py —
тот же паттерн уже проверен в этом репозитории).
"""

from __future__ import annotations

import inspect
from datetime import datetime
from datetime import timedelta

import pytest
from sqlalchemy import func
from sqlalchemy import select

from bot.config import settings
from bot.services import economy_service
from common.models.chat_bank import ChatBank
from common.models.economy_tx import EconomyTx
from common.models.market import Market
from common.models.user import User
from common.models.user_balance import UserBalance


async def _ensure_user(session, user_id: int, first_name: str = "Тест") -> None:
    session.add(User(id=user_id, first_name=first_name))
    await session.flush()


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


async def _count_tx(session, chat_id: int, user_id: int | None, kind: str) -> int:
    result = await session.execute(
        select(func.count()).select_from(EconomyTx).where(
            EconomyTx.chat_id == chat_id, EconomyTx.user_id == user_id, EconomyTx.kind == kind
        )
    )
    return result.scalar_one()


async def _count_tx_by_ref(session, chat_id: int, ref_id: str) -> int:
    result = await session.execute(
        select(func.count()).select_from(EconomyTx).where(
            EconomyTx.chat_id == chat_id, EconomyTx.ref_id == ref_id
        )
    )
    return result.scalar_one()


# --- get_balance / стартовый бонус (ECON-01) --------------------------------


@pytest.mark.asyncio
async def test_get_or_create_balance_grants_start_bonus_once(session):
    chat_id = -100700001
    user_id = 800001
    await _ensure_user(session, user_id)

    balance1 = await economy_service.get_balance(session, chat_id, user_id)
    assert balance1 == settings.economy_start_bonus

    balance2 = await economy_service.get_balance(session, chat_id, user_id)
    assert balance2 == settings.economy_start_bonus

    count = await _count_tx(session, chat_id, user_id, kind="start_bonus")
    assert count == 1


# --- transfer_with_fee (D-04, ECON-03) --------------------------------------


@pytest.mark.asyncio
async def test_transfer_with_fee_moves_money_and_fee_to_bank(session):
    chat_id = -100700002
    sender, receiver = 800010, 800011
    await _ensure_user(session, sender)
    await _ensure_user(session, receiver)
    await economy_service.get_balance(session, chat_id, sender)
    await economy_service.get_balance(session, chat_id, receiver)

    ref_id = "test_transfer_moves_money"
    await economy_service.transfer_with_fee(session, chat_id, sender, receiver, 100, ref_id)

    assert await _get_user_balance(session, chat_id, sender) == 1000 - 100
    assert await _get_user_balance(session, chat_id, receiver) == 1000 + 95
    assert await _get_bank_balance(session, chat_id) == 5
    assert await _count_tx_by_ref(session, chat_id, ref_id) == 3


@pytest.mark.asyncio
async def test_transfer_fee_floor_is_one(session):
    chat_id = -100700003
    sender, receiver = 800020, 800021
    await _ensure_user(session, sender)
    await _ensure_user(session, receiver)
    await economy_service.get_balance(session, chat_id, sender)
    await economy_service.get_balance(session, chat_id, receiver)

    await economy_service.transfer_with_fee(session, chat_id, sender, receiver, 1, "test_transfer_floor")

    assert await _get_user_balance(session, chat_id, receiver) == 1000  # amount(1) - fee(1) = 0
    assert await _get_bank_balance(session, chat_id) == 1


@pytest.mark.asyncio
async def test_transfer_with_fee_idempotent_on_retry(session):
    chat_id = -100700004
    sender, receiver = 800030, 800031
    await _ensure_user(session, sender)
    await _ensure_user(session, receiver)
    await economy_service.get_balance(session, chat_id, sender)
    await economy_service.get_balance(session, chat_id, receiver)

    ref_id = "test_transfer_idempotent_retry"
    await economy_service.transfer_with_fee(session, chat_id, sender, receiver, 100, ref_id)
    after_first = (
        await _get_user_balance(session, chat_id, sender),
        await _get_user_balance(session, chat_id, receiver),
        await _get_bank_balance(session, chat_id),
    )

    await economy_service.transfer_with_fee(session, chat_id, sender, receiver, 100, ref_id)
    after_second = (
        await _get_user_balance(session, chat_id, sender),
        await _get_user_balance(session, chat_id, receiver),
        await _get_bank_balance(session, chat_id),
    )

    assert after_first == after_second
    assert await _count_tx_by_ref(session, chat_id, ref_id) == 3


@pytest.mark.asyncio
async def test_transfer_insufficient_funds_raises(session):
    chat_id = -100700005
    sender, receiver = 800040, 800041
    await _ensure_user(session, sender)
    await _ensure_user(session, receiver)
    await economy_service.get_balance(session, chat_id, sender)
    await economy_service.get_balance(session, chat_id, receiver)

    with pytest.raises(economy_service.InsufficientFunds):
        await economy_service.transfer_with_fee(
            session, chat_id, sender, receiver, 999_999, "test_transfer_insufficient"
        )

    assert await _get_user_balance(session, chat_id, sender) == 1000
    assert await _get_user_balance(session, chat_id, receiver) == 1000


@pytest.mark.asyncio
async def test_transfer_self_or_nonpositive_raises(session):
    chat_id = -100700006
    user_id, other_id = 800050, 800051
    await _ensure_user(session, user_id)
    await _ensure_user(session, other_id)
    await economy_service.get_balance(session, chat_id, user_id)

    with pytest.raises(economy_service.InvalidArgument):
        await economy_service.transfer_with_fee(session, chat_id, user_id, user_id, 10, "test_transfer_self")

    with pytest.raises(economy_service.InvalidArgument):
        await economy_service.transfer_with_fee(
            session, chat_id, user_id, other_id, 0, "test_transfer_nonpositive"
        )


# --- debit (ECON-03 идемпотентность) ----------------------------------------


@pytest.mark.asyncio
async def test_debit_idempotent_on_duplicate_ref(session):
    chat_id = -100700007
    user_id = 800060
    await _ensure_user(session, user_id)
    await economy_service.get_balance(session, chat_id, user_id)  # 1000

    ref_id = "test_debit_duplicate_ref"
    ok_first = await economy_service.debit(session, chat_id, user_id, 100, "bet_place", ref_id)
    assert ok_first is True

    ok_second = await economy_service.debit(session, chat_id, user_id, 100, "bet_place", ref_id)
    assert ok_second is False

    assert await _get_user_balance(session, chat_id, user_id) == 900


@pytest.mark.asyncio
async def test_debit_insufficient_raises(session):
    chat_id = -100700008
    user_id = 800070
    await _ensure_user(session, user_id)
    await economy_service.get_balance(session, chat_id, user_id)  # 1000

    with pytest.raises(economy_service.InsufficientFunds):
        await economy_service.debit(session, chat_id, user_id, 999_999, "bet_place", "test_debit_insufficient")

    assert await _get_user_balance(session, chat_id, user_id) == 1000


# --- debit_to_bank (IN-01: общий стейк-примитив казино/дуэлей) --------------


@pytest.mark.asyncio
async def test_debit_to_bank_moves_stake_to_shared_bank(session):
    chat_id = -100700009
    user_id = 800080
    await _ensure_user(session, user_id)
    await economy_service.get_balance(session, chat_id, user_id)  # 1000
    bank_before = await _get_bank_balance(session, chat_id)

    ok = await economy_service.debit_to_bank(
        session, chat_id, user_id, 100, "casino_bet", "test_debit_to_bank_ref"
    )
    await session.commit()

    assert ok is True
    assert await _get_user_balance(session, chat_id, user_id) == 900
    assert await _get_bank_balance(session, chat_id) == bank_before + 100


@pytest.mark.asyncio
async def test_debit_to_bank_idempotent_skips_bank_leg_on_replay(session):
    chat_id = -100700010
    user_id = 800081
    await _ensure_user(session, user_id)
    await economy_service.get_balance(session, chat_id, user_id)  # 1000

    ref_id = "test_debit_to_bank_replay"
    first = await economy_service.debit_to_bank(session, chat_id, user_id, 100, "casino_bet", ref_id)
    await session.commit()
    assert first is True
    bank_after_first = await _get_bank_balance(session, chat_id)

    second = await economy_service.debit_to_bank(session, chat_id, user_id, 100, "casino_bet", ref_id)
    await session.commit()

    assert second is False
    assert await _get_user_balance(session, chat_id, user_id) == 900
    assert await _get_bank_balance(session, chat_id) == bank_after_first


# --- leaderboard / chat summary (D-06) --------------------------------------


@pytest.mark.asyncio
async def test_get_leaderboard_orders_by_balance_desc(session):
    chat_id = -100700009
    u1, u2, u3 = 800080, 800081, 800082
    await _ensure_user(session, u1, "Алиса")
    await _ensure_user(session, u2, "Боб")
    await _ensure_user(session, u3, "Вера")

    await economy_service.get_balance(session, chat_id, u1)
    await economy_service.get_balance(session, chat_id, u2)
    await economy_service.get_balance(session, chat_id, u3)

    assert await economy_service.credit(session, chat_id, u2, 500, "test_credit", "test_leaderboard_credit_u2")
    assert await economy_service.debit(session, chat_id, u3, 200, "test_debit", "test_leaderboard_debit_u3")
    await session.commit()

    leaderboard = await economy_service.get_leaderboard(session, chat_id, limit=10)

    balances = [row["balance"] for row in leaderboard]
    assert balances == sorted(balances, reverse=True)
    assert leaderboard[0]["user_id"] == u2
    assert leaderboard[0]["balance"] == 1500
    assert leaderboard[-1]["user_id"] == u3
    assert leaderboard[-1]["balance"] == 800


@pytest.mark.asyncio
async def test_get_chat_summary_fields(session):
    chat_id = -100700010
    u1, u2 = 800090, 800091
    await _ensure_user(session, u1)
    await _ensure_user(session, u2)
    await economy_service.get_balance(session, chat_id, u1)
    await economy_service.get_balance(session, chat_id, u2)

    await economy_service.transfer_with_fee(session, chat_id, u1, u2, 100, "test_summary_transfer")

    market = Market(
        chat_id=chat_id,
        type="internal",
        question="Тестовый рынок для сводки?",
        creator_id=u1,
        status="open",
        closes_at=datetime.utcnow() + timedelta(days=1),
    )
    session.add(market)
    await session.flush()

    summary = await economy_service.get_chat_summary(session, chat_id)

    assert summary["bank_balance"] == 5
    assert summary["total_in_circulation"] == (1000 - 100) + (1000 + 95)
    assert summary["open_markets_count"] == 1


# --- get_transactions (04.2-08, Pitfall 6: history-feed read) --------------


@pytest.mark.asyncio
async def test_get_transactions_orders_desc_and_filters_by_user(session):
    chat_id = -100700020
    u1, u2 = 800100, 800101
    await _ensure_user(session, u1)
    await _ensure_user(session, u2)
    await economy_service.get_balance(session, chat_id, u1)  # start_bonus tx
    await economy_service.get_balance(session, chat_id, u2)

    assert await economy_service.credit(session, chat_id, u1, 50, "test_credit", "test_tx_u1_a")
    assert await economy_service.credit(session, chat_id, u1, 30, "test_credit", "test_tx_u1_b")
    assert await economy_service.credit(session, chat_id, u2, 20, "test_credit", "test_tx_u2_a")
    await session.commit()

    rows = await economy_service.get_transactions(session, chat_id, user_id=u1)

    assert len(rows) == 3  # start_bonus + 2 credits, none belonging to u2
    assert all(row["user_id"] == u1 for row in rows)
    created_ats = [row["created_at"] for row in rows]
    assert created_ats == sorted(created_ats, reverse=True)
    assert {row["kind"] for row in rows} == {"start_bonus", "test_credit"}


@pytest.mark.asyncio
async def test_get_transactions_pagination(session):
    chat_id = -100700021
    user_id = 800110
    await _ensure_user(session, user_id)
    await economy_service.get_balance(session, chat_id, user_id)
    for i in range(5):
        assert await economy_service.credit(
            session, chat_id, user_id, 1, "test_credit", f"test_tx_page_{i}"
        )
    await session.commit()

    page1 = await economy_service.get_transactions(session, chat_id, user_id=user_id, limit=2, offset=0)
    page2 = await economy_service.get_transactions(session, chat_id, user_id=user_id, limit=2, offset=2)

    assert len(page1) == 2
    assert len(page2) == 2
    assert {row["id"] for row in page1}.isdisjoint({row["id"] for row in page2})


@pytest.mark.asyncio
async def test_get_transactions_hides_bank_mirror_kinds_in_chatwide_feed(session):
    chat_id = -100700022
    sender, receiver = 800120, 800121
    await _ensure_user(session, sender)
    await _ensure_user(session, receiver)
    await economy_service.get_balance(session, chat_id, sender)
    await economy_service.get_balance(session, chat_id, receiver)

    await economy_service.transfer_with_fee(session, chat_id, sender, receiver, 100, "test_tx_hidden_fee")

    # user_id=None -> chat-wide feed; the transfer_fee bank-only mirror leg
    # (user_id IS NULL) must be filtered, while the meaningful transfer_out/
    # transfer_in legs (real user_id) stay visible.
    rows = await economy_service.get_transactions(session, chat_id, user_id=None, limit=100)

    kinds = [row["kind"] for row in rows]
    assert "transfer_fee" not in kinds
    assert "transfer_out" in kinds
    assert "transfer_in" in kinds


@pytest.mark.asyncio
async def test_get_transactions_is_read_only_no_writes():
    """Статическая проверка: get_transactions не трогает _log_tx/_credit/
    _guarded_debit ни в каком виде — чистое чтение (Pitfall 6)."""
    source = inspect.getsource(economy_service.get_transactions)
    assert "_log_tx(" not in source
    assert "_credit(" not in source
    assert "_guarded_debit(" not in source
    assert "session.commit()" not in source


# --- append-only инвариант (ECON-03) ----------------------------------------


def test_economy_tx_is_append_only_static():
    """Статическая проверка исходника: economy_service никогда не делает
    update(EconomyTx)/delete(EconomyTx) — economy_tx только INSERT."""
    source = inspect.getsource(economy_service)
    assert "update(EconomyTx" not in source
    assert "delete(EconomyTx" not in source
    assert "UPDATE economy_tx" not in source
    assert "DELETE FROM economy_tx" not in source
