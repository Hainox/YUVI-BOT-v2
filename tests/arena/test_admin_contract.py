from __future__ import annotations

import os
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

os.environ.setdefault("BOT_TOKEN", "test-token")
os.environ.setdefault("CHAT_ID", "-700001")
os.environ.setdefault("OWNER_ID", "1")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost:5432/test")

import pytest

from api.deps import AuthContext
from api.routes import arena_admin


class _Result:
    def __init__(self, *, rows=(), scalar=None, one=None):
        self._rows = rows
        self._scalar = scalar
        self._one = one

    def all(self):
        return list(self._rows)

    def scalar_one_or_none(self):
        return self._scalar

    def one(self):
        return self._one

    def scalars(self):
        return self


def _session_context(results):
    class Context:
        async def __aenter__(self):
            session = AsyncMock()
            session.execute = AsyncMock(side_effect=results)
            return session

        async def __aexit__(self, exc_type, exc, tb):
            return False

    return Context()


@pytest.mark.asyncio
async def test_admin_overview_is_chat_scoped_and_reports_fund_totals(monkeypatch):
    monkeypatch.setattr(
        arena_admin,
        "SessionLocal",
        lambda: _session_context(
            [
                _Result(rows=[("active", 2), ("finished", 5), ("waiting", 1)]),
                _Result(scalar=1250),
                _Result(one=(8, 1250)),
            ]
        ),
    )

    result = await arena_admin.get_arena_admin_overview(
        AuthContext(user_id=9, chat_id=-700001, status="administrator")
    )

    assert result == {
        "chat_id": -700001,
        "matches": {"active": 2, "finished": 5, "waiting": 1},
        "unresolved_matches": 3,
        "settlement_pending_matches": 2,
        "fund": {"balance": 1250, "ledger_entries": 8, "ledger_total": 1250},
    }


@pytest.mark.asyncio
async def test_admin_fund_ledger_serializes_newest_rows_with_pagination(monkeypatch):
    row = SimpleNamespace(
        id=4,
        chat_id=-700001,
        amount=250,
        balance_after=1250,
        kind="arena_match_fee",
        idempotency_key="arena:match:7:settle:fund_fee",
        match_id=7,
        note=None,
        created_at=datetime(2026, 8, 8, tzinfo=timezone.utc),
    )
    monkeypatch.setattr(
        arena_admin,
        "SessionLocal",
        lambda: _session_context([_Result(rows=[row])]),
    )

    result = await arena_admin.get_arena_admin_fund_ledger(
        limit=10,
        offset=20,
        auth=AuthContext(user_id=9, chat_id=-700001, status="administrator"),
    )

    assert result == [
        {
            "id": 4,
            "chat_id": -700001,
            "amount": 250,
            "balance_after": 1250,
            "kind": "arena_match_fee",
            "idempotency_key": "arena:match:7:settle:fund_fee",
            "match_id": 7,
            "note": None,
            "created_at": datetime(2026, 8, 8, tzinfo=timezone.utc),
        }
    ]
