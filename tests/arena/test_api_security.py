from __future__ import annotations

import os
from datetime import datetime
from unittest.mock import AsyncMock

os.environ.setdefault("BOT_TOKEN", "test-token")
os.environ.setdefault("CHAT_ID", "-700001")
os.environ.setdefault("OWNER_ID", "1")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost:5432/test")

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from api.deps import AuthContext
from api.routes import arena as arena_route
from bot.services import arena_service
from common.arena.schemas import FighterType
from common.models.arena import ArenaMatch


class _State:
    redis = None


class _App:
    state = _State()


def _request() -> Request:
    return Request({"type": "http", "app": _App(), "headers": [], "query_string": b""})


def _session_context():
    class Context:
        async def __aenter__(self):
            return AsyncMock()

        async def __aexit__(self, exc_type, exc, tb):
            return False

    return Context()


def _own_match() -> ArenaMatch:
    return ArenaMatch(
        id=42,
        chat_id=-700001,
        creator_id=202,
        creator_fighter=FighterType.TANK.value,
        creator_bet=100,
        creation_idempotency_key="create-42",
        opponent_id=None,
        opponent_fighter=None,
        opponent_bet=None,
        status="waiting",
        settlement_status="pending",
        created_at=datetime.utcnow(),
        expires_at=datetime.utcnow(),
    )


@pytest.mark.asyncio
async def test_accept_route_uses_auth_user_and_maps_not_found(monkeypatch):
    monkeypatch.setattr(arena_route, "SessionLocal", lambda: _session_context())
    monkeypatch.setattr(
        arena_route.arena_service,
        "accept_match",
        AsyncMock(side_effect=arena_service.MatchNotFound("missing")),
    )

    with pytest.raises(HTTPException) as error:
        await arena_route.post_accept_arena_match(
            999,
            arena_route.AcceptArenaMatchBody(
                fighter="assassin", bet=100, idempotency_key="accept-1"
            ),
            _request(),
            AuthContext(user_id=202, chat_id=-700001, status="member"),
        )

    assert error.value.status_code == 404


@pytest.mark.asyncio
async def test_confirm_route_never_accepts_actor_from_body(monkeypatch):
    confirm = AsyncMock(side_effect=arena_service.MatchConflict("not participant"))
    monkeypatch.setattr(arena_route.arena_service, "confirm_match", confirm)
    monkeypatch.setattr(arena_route, "SessionLocal", lambda: _session_context())

    with pytest.raises(HTTPException) as error:
        await arena_route.post_confirm_arena_match(
            7,
            _request(),
            AuthContext(user_id=999, chat_id=-700001, status="member"),
        )

    assert error.value.status_code == 409
    assert confirm.await_args.args[3] == 999


@pytest.mark.asyncio
async def test_my_matches_uses_verified_user_and_serializes_only_own_fighter(monkeypatch):
    list_user_matches = AsyncMock(return_value=[_own_match()])
    monkeypatch.setattr(arena_route.arena_service, "list_user_matches", list_user_matches)
    monkeypatch.setattr(arena_route, "SessionLocal", lambda: _session_context())

    result = await arena_route.get_my_arena_matches(
        AuthContext(user_id=202, chat_id=-700001, status="member")
    )

    assert result[0]["id"] == 42
    assert result[0]["creator_fighter"] == FighterType.TANK.value
    assert "opponent_fighter" not in result[0]
    assert list_user_matches.await_args.args[2] == 202
