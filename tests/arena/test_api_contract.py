from __future__ import annotations

import os

os.environ.setdefault("BOT_TOKEN", "test-token")
os.environ.setdefault("CHAT_ID", "-700001")
os.environ.setdefault("OWNER_ID", "1")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost:5432/test")

from datetime import datetime

from api.routes.arena import _serialize_match
from common.arena.schemas import FighterType
from common.models.arena import ArenaMatch


def _match(status: str) -> ArenaMatch:
    return ArenaMatch(
        id=1,
        chat_id=-700001,
        creator_id=101,
        creator_fighter=FighterType.TANK.value,
        creator_bet=100,
        creation_idempotency_key="k",
        opponent_id=202,
        opponent_fighter=FighterType.ASSASSIN.value,
        opponent_bet=150,
        creator_confirmed=False,
        opponent_confirmed=False,
        status=status,
        settlement_status="pending",
        created_at=datetime.utcnow(),
        expires_at=datetime.utcnow(),
    )


def test_lobby_hides_opponent_fighter_before_active() -> None:
    creator_view = _serialize_match(_match("accepting"), viewer_id=101)
    opponent_view = _serialize_match(_match("accepting"), viewer_id=202)
    public_view = _serialize_match(_match("accepting"), viewer_id=303)

    assert creator_view["creator_fighter"] == FighterType.TANK.value
    assert "opponent_fighter" not in creator_view
    assert opponent_view["opponent_fighter"] == FighterType.ASSASSIN.value
    assert "creator_fighter" not in opponent_view
    assert "creator_fighter" not in public_view
    assert "opponent_fighter" not in public_view


def test_active_match_reveals_both_fighters() -> None:
    payload = _serialize_match(_match("active"), viewer_id=303)

    assert payload["creator_fighter"] == FighterType.TANK.value
    assert payload["opponent_fighter"] == FighterType.ASSASSIN.value
    assert "user_balance_after" not in payload
