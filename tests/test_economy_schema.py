"""Интеграционный смоук-тест схемы миграции 0004 против живого Postgres
(фикстура `session` из tests/conftest.py — транзакция-на-тест).

Доказывает:
- все 6 таблиц экономики/рынков ставок реально существуют после `alembic upgrade head`;
- частичные UNIQUE-индексы ux_economy_tx_ref_id_kind и ux_markets_chat_type_external на месте;
- CHECK (balance >= 0) на user_balance реально блокирует отрицательный баланс;
- chat_bank НЕ имеет такого CHECK — казино Фазы 4 сможет уводить банк в минус.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

_EXPECTED_TABLES = {
    "user_balance",
    "chat_bank",
    "economy_tx",
    "markets",
    "market_options",
    "bets",
}


@pytest.mark.asyncio
async def test_all_six_tables_exist(session) -> None:
    result = await session.execute(
        text(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_name = ANY(:names)"
        ),
        {"names": list(_EXPECTED_TABLES)},
    )
    found = {row[0] for row in result.all()}
    assert found == _EXPECTED_TABLES


@pytest.mark.asyncio
async def test_partial_unique_indexes_exist(session) -> None:
    result = await session.execute(
        text(
            "SELECT indexname FROM pg_indexes WHERE schemaname = 'public' "
            "AND indexname IN ('ux_economy_tx_ref_id_kind', 'ux_markets_chat_type_external')"
        )
    )
    found = {row[0] for row in result.all()}
    assert found == {"ux_economy_tx_ref_id_kind", "ux_markets_chat_type_external"}


@pytest.mark.asyncio
async def test_user_balance_check_constraint_rejects_negative(session) -> None:
    await session.execute(text("INSERT INTO users (id, first_name) VALUES (999001, 'Тест')"))
    await session.flush()

    with pytest.raises(IntegrityError):
        await session.execute(
            text(
                "INSERT INTO user_balance (chat_id, user_id, balance) "
                "VALUES (-100, 999001, -1)"
            )
        )
        await session.flush()


@pytest.mark.asyncio
async def test_chat_bank_allows_negative_balance(session) -> None:
    # chat_bank намеренно БЕЗ CHECK (balance >= 0) — казино Фазы 4 уводит банк в минус.
    await session.execute(text("INSERT INTO chat_bank (chat_id, balance) VALUES (-101, -1)"))
    await session.flush()

    result = await session.execute(
        text("SELECT balance FROM chat_bank WHERE chat_id = -101")
    )
    assert result.scalar_one() == -1
