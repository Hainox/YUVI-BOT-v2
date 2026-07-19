"""Интеграционный смоук-тест схемы миграции 0008 против живого Postgres
(фикстура `session` из tests/conftest.py — транзакция-на-тест).

Доказывает:
- все 3 новые таблицы (daily_picks/active_titles/twin_opt_ins) реально
  существуют после `alembic upgrade head`;
- daily_stats принимает 4 новые колонки (insert + select);
- daily_picks идемпотентен по (chat_id, kind, day_msk) — UNIQUE отвергает
  повтор пика в тот же MSK-день (D-09);
- active_titles допускает ровно один активный титул на участника —
  частичный UNIQUE(chat_id, user_id) WHERE status='active' реально отвергает
  вторую active-строку того же user_id, но пропускает несколько
  suspended/expired строк (D-10);
- twin_opt_ins уникален по (chat_id, user_id) (TWIN-02).
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

_EXPECTED_TABLES = {"daily_picks", "active_titles", "twin_opt_ins"}


@pytest.mark.asyncio
async def test_three_tables_exist(session) -> None:
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
async def test_daily_stats_has_four_new_columns(session) -> None:
    await session.execute(text("INSERT INTO users (id, first_name) VALUES (900501, 'Тест')"))
    await session.flush()

    await session.execute(
        text(
            "INSERT INTO daily_stats "
            "(chat_id, user_id, stat_date, message_count, profanity_count, "
            "photo_count, forward_count, longest_msg_len) "
            "VALUES (-900501, 900501, '2026-07-19', 3, 1, 2, 1, 240)"
        )
    )
    await session.flush()

    result = await session.execute(
        text(
            "SELECT profanity_count, photo_count, forward_count, longest_msg_len "
            "FROM daily_stats WHERE chat_id = -900501 AND user_id = 900501"
        )
    )
    row = result.one()
    assert row == (1, 2, 1, 240)


@pytest.mark.asyncio
async def test_daily_pick_idempotent_per_day(session) -> None:
    await session.execute(text("INSERT INTO users (id, first_name) VALUES (900502, 'Тест2')"))
    await session.flush()

    await session.execute(
        text(
            "INSERT INTO daily_picks (chat_id, kind, day_msk, winner_user_id) "
            "VALUES (-900502, 'victim', '2026-07-19', 900502)"
        )
    )
    await session.flush()

    with pytest.raises(IntegrityError):
        await session.execute(
            text(
                "INSERT INTO daily_picks (chat_id, kind, day_msk, winner_user_id) "
                "VALUES (-900502, 'victim', '2026-07-19', 900502)"
            )
        )
        await session.flush()


@pytest.mark.asyncio
async def test_active_title_rejects_second_active_row_per_user(session) -> None:
    await session.execute(text("INSERT INTO users (id, first_name) VALUES (900503, 'Тест3')"))
    await session.flush()

    await session.execute(
        text(
            "INSERT INTO active_titles "
            "(chat_id, user_id, tg_user_id, title, source, status) "
            "VALUES (-900503, 900503, 900503, 'Жертва', 'victim', 'active')"
        )
    )
    await session.flush()

    with pytest.raises(IntegrityError):
        await session.execute(
            text(
                "INSERT INTO active_titles "
                "(chat_id, user_id, tg_user_id, title, source, status) "
                "VALUES (-900503, 900503, 900503, 'Арендатор', 'rental', 'active')"
            )
        )
        await session.flush()


@pytest.mark.asyncio
async def test_active_title_allows_multiple_non_active_rows_per_user(session) -> None:
    await session.execute(text("INSERT INTO users (id, first_name) VALUES (900504, 'Тест4')"))
    await session.flush()

    for status in ("suspended", "expired"):
        await session.execute(
            text(
                "INSERT INTO active_titles "
                "(chat_id, user_id, tg_user_id, title, source, status) "
                "VALUES (-900504, 900504, 900504, 'Тег', 'rental', :status)"
            ),
            {"status": status},
        )
    await session.flush()

    result = await session.execute(
        text("SELECT count(*) FROM active_titles WHERE chat_id = -900504 AND user_id = 900504")
    )
    assert result.scalar_one() == 2


@pytest.mark.asyncio
async def test_twin_opt_in_unique_per_chat_user(session) -> None:
    await session.execute(text("INSERT INTO users (id, first_name) VALUES (900505, 'Тест5')"))
    await session.flush()

    await session.execute(
        text(
            "INSERT INTO twin_opt_ins (chat_id, user_id, status) "
            "VALUES (-900505, 900505, 'active')"
        )
    )
    await session.flush()

    with pytest.raises(IntegrityError):
        await session.execute(
            text(
                "INSERT INTO twin_opt_ins (chat_id, user_id, status) "
                "VALUES (-900505, 900505, 'paused')"
            )
        )
        await session.flush()
