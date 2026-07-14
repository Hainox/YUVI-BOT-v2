"""Интеграционный смоук-тест схемы миграции 0005 против живого Postgres
(фикстура `session` из tests/conftest.py — транзакция-на-тест).

Доказывает:
- все 6 таблиц игрового слоя реально существуют после `alembic upgrade head`;
- частичный UNIQUE-индекс ux_casino_games_user_idem на месте;
- этот индекс реально отвергает повтор (user_id, idem_key), но пропускает
  сколько угодно строк с idem_key IS NULL (partial-индекс не ловит NULL).
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

_EXPECTED_TABLES = {
    "casino_games",
    "clicker_farms",
    "clicker_market_pool",
    "clicker_market_price",
    "duels",
    "gacha_collection",
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
async def test_partial_unique_index_exists(session) -> None:
    result = await session.execute(
        text(
            "SELECT indexname FROM pg_indexes WHERE schemaname = 'public' "
            "AND indexname = 'ux_casino_games_user_idem'"
        )
    )
    assert result.scalar_one_or_none() == "ux_casino_games_user_idem"


@pytest.mark.asyncio
async def test_idem_key_replay_rejected(session) -> None:
    await session.execute(text("INSERT INTO users (id, first_name) VALUES (999002, 'Тест')"))
    await session.flush()

    await session.execute(
        text(
            "INSERT INTO casino_games (chat_id, user_id, game, bet, idem_key) "
            "VALUES (-1, 999002, 'dice', 100, 'round-1')"
        )
    )
    await session.flush()

    with pytest.raises(IntegrityError):
        await session.execute(
            text(
                "INSERT INTO casino_games (chat_id, user_id, game, bet, idem_key) "
                "VALUES (-1, 999002, 'dice', 100, 'round-1')"
            )
        )
        await session.flush()


@pytest.mark.asyncio
async def test_null_idem_key_allows_duplicates(session) -> None:
    await session.execute(text("INSERT INTO users (id, first_name) VALUES (999003, 'Тест2')"))
    await session.flush()

    for _ in range(2):
        await session.execute(
            text(
                "INSERT INTO casino_games (chat_id, user_id, game, bet, idem_key) "
                "VALUES (-1, 999003, 'coinflip', 50, NULL)"
            )
        )
    await session.flush()

    result = await session.execute(
        text("SELECT count(*) FROM casino_games WHERE user_id = 999003 AND idem_key IS NULL")
    )
    assert result.scalar_one() == 2
