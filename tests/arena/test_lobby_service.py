from __future__ import annotations

from datetime import datetime
from datetime import timedelta
from datetime import timezone
from unittest.mock import AsyncMock
from unittest.mock import Mock

import pytest

from bot.services import arena_service
from common.arena.schemas import FighterType
from common.models.arena import ArenaMatch


CHAT_ID = -700001
CREATOR_ID = 101
OPPONENT_ID = 202


def _match(**overrides) -> ArenaMatch:
    values = {
        "id": 7,
        "chat_id": CHAT_ID,
        "creator_id": CREATOR_ID,
        "creator_fighter": FighterType.TANK.value,
        "creator_bet": 100,
        "creation_idempotency_key": "create-1",
        "opponent_id": None,
        "opponent_fighter": None,
        "opponent_bet": None,
        "creator_confirmed": False,
        "opponent_confirmed": False,
        "status": "waiting",
        "settlement_status": "pending",
        "created_at": datetime.utcnow(),
        "expires_at": datetime.utcnow() + timedelta(minutes=5),
    }
    values.update(overrides)
    return ArenaMatch(**values)


@pytest.fixture
def session() -> AsyncMock:
    value = AsyncMock()
    value.add = Mock()
    value.flush = AsyncMock()
    value.commit = AsyncMock()
    return value


@pytest.mark.asyncio
async def test_create_match_escrows_creator_and_hides_fighter(session, monkeypatch):
    debit = AsyncMock(return_value=True)
    monkeypatch.setattr(arena_service.economy_service, "debit", debit)
    match = await arena_service.create_match(
        session,
        CHAT_ID,
        CREATOR_ID,
        FighterType.TANK,
        100,
        "create-1",
        now=datetime(2026, 8, 7, 12, 0, 0),
    )

    debit.assert_awaited_once_with(
        session,
        CHAT_ID,
        CREATOR_ID,
        100,
        kind="arena_match_creator_bet",
        ref_id="arena:match:create-1:creator",
    )
    session.add.assert_called_once()
    session.commit.assert_awaited_once()
    assert match.status == "waiting"
    assert match.creator_fighter == FighterType.TANK.value
    assert match.opponent_id is None


@pytest.mark.asyncio
async def test_create_match_rejects_replayed_escrow(session, monkeypatch):
    monkeypatch.setattr(arena_service.economy_service, "debit", AsyncMock(return_value=False))

    with pytest.raises(arena_service.DuplicateRequest):
        await arena_service.create_match(
            session, CHAT_ID, CREATOR_ID, FighterType.TANK, 100, "same-ref"
        )

    session.add.assert_not_called()
    session.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_accept_escrows_opponent_and_enters_accepting(session, monkeypatch):
    match = _match()
    monkeypatch.setattr(arena_service, "_get_match_for_update", AsyncMock(return_value=match))
    debit = AsyncMock(return_value=True)
    monkeypatch.setattr(arena_service.economy_service, "debit", debit)

    result = await arena_service.accept_match(
        session,
        CHAT_ID,
        match.id,
        OPPONENT_ID,
        FighterType.ASSASSIN,
        150,
        "accept-1",
        now=datetime(2026, 8, 7, 12, 0, 0),
    )

    debit.assert_awaited_once_with(
        session,
        CHAT_ID,
        OPPONENT_ID,
        150,
        kind="arena_match_opponent_bet",
        ref_id="arena:match:7:opponent:accept-1",
    )
    assert result.status == "accepting"
    assert result.opponent_id == OPPONENT_ID
    assert result.opponent_fighter == FighterType.ASSASSIN.value
    assert result.opponent_bet == 150
    assert result.accept_deadline == datetime(2026, 8, 7, 12, 0, 15, tzinfo=timezone.utc)
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_cannot_accept_own_match(session, monkeypatch):
    match = _match()
    monkeypatch.setattr(arena_service, "_get_match_for_update", AsyncMock(return_value=match))

    with pytest.raises(arena_service.MatchConflict, match="самого себя"):
        await arena_service.accept_match(
            session, CHAT_ID, match.id, CREATOR_ID, FighterType.ASSASSIN, 100, "accept-1"
        )


@pytest.mark.asyncio
async def test_confirm_both_players_starts_match(session, monkeypatch):
    match = _match(
        status="accepting",
        opponent_id=OPPONENT_ID,
        opponent_fighter=FighterType.ASSASSIN.value,
        opponent_bet=150,
        accept_deadline=datetime.utcnow() + timedelta(seconds=15),
    )
    monkeypatch.setattr(arena_service, "_get_match_for_update", AsyncMock(return_value=match))

    first = await arena_service.confirm_match(session, CHAT_ID, match.id, OPPONENT_ID)
    assert first.opponent_confirmed is True
    assert first.status == "accepting"

    match.creator_confirmed = True
    second = await arena_service.confirm_match(session, CHAT_ID, match.id, CREATOR_ID)
    assert second.creator_confirmed is True
    assert second.status == "active"
    assert second.started_at is not None


@pytest.mark.asyncio
async def test_cancel_refunds_creator_once(session, monkeypatch):
    match = _match()
    monkeypatch.setattr(arena_service, "_get_match_for_update", AsyncMock(return_value=match))
    credit = AsyncMock(return_value=True)
    monkeypatch.setattr(arena_service.economy_service, "credit", credit)

    result = await arena_service.cancel_match(session, CHAT_ID, match.id, CREATOR_ID)

    credit.assert_awaited_once_with(
        session,
        CHAT_ID,
        CREATOR_ID,
        100,
        kind="arena_match_refund",
        ref_id="arena:match:7:creator:refund",
    )
    assert result.status == "cancelled"
    assert result.settlement_status == "refunded"


@pytest.mark.asyncio
async def test_expire_accepting_refunds_both_stakes(session, monkeypatch):
    match = _match(
        status="accepting",
        opponent_id=OPPONENT_ID,
        opponent_fighter=FighterType.ASSASSIN.value,
        opponent_bet=150,
        accept_deadline=datetime(2026, 8, 7, 11, 59, 59),
    )
    monkeypatch.setattr(arena_service, "_get_match_for_update", AsyncMock(return_value=match))
    credit = AsyncMock(return_value=True)
    monkeypatch.setattr(arena_service.economy_service, "credit", credit)

    result = await arena_service.expire_match(
        session, CHAT_ID, match.id, now=datetime(2026, 8, 7, 12, 0, 0)
    )

    assert result.status == "refunded"
    assert result.settlement_status == "refunded"
    assert credit.await_count == 2
    assert {call.kwargs["ref_id"] for call in credit.await_args_list} == {
        "arena:match:7:creator:refund",
        "arena:match:7:opponent:refund",
    }
