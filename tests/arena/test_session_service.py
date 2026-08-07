from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from bot.services.arena_session_service import ArenaSessionService
from bot.services.arena_session_service import DuplicateAction
from bot.services.arena_session_service import SessionConflict
from common.arena.schemas import ArenaPlayerAction
from common.arena.schemas import FighterType


class _Lock:
    async def acquire(self, *args, **kwargs):
        return True

    async def release(self):
        return None

    async def __aenter__(self):
        await self.acquire()
        return self

    async def __aexit__(self, *args):
        await self.release()


class FakeRedis:
    def __init__(self):
        self.data: dict[str, str] = {}
        self.published: list[tuple[str, str]] = []

    async def get(self, key: str):
        return self.data.get(key)

    async def set(self, key: str, value: str, **kwargs):
        self.data[key] = value
        return True

    async def publish(self, channel: str, payload: str):
        self.published.append((channel, payload))
        return 1

    def lock(self, key: str, **kwargs):
        return _Lock()


def _match(**overrides):
    values = {
        "id": 11,
        "chat_id": -700001,
        "creator_id": 101,
        "creator_fighter": FighterType.TANK.value,
        "opponent_id": 202,
        "opponent_fighter": FighterType.ASSASSIN.value,
        "status": "active",
        "started_at": datetime(2026, 8, 7, 12, 0, tzinfo=timezone.utc),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.fixture
def service():
    return ArenaSessionService(FakeRedis())


@pytest.mark.asyncio
async def test_start_creates_server_clock_and_hides_client_control(service):
    state = await service.start_or_get(
        _match(), now=datetime(2026, 8, 7, 12, 0, tzinfo=timezone.utc)
    )

    assert state["phase_index"] == 0
    assert state["phase_deadline"] == "2026-08-07T12:00:03+00:00"
    public = service.public_state(state, 202)
    assert public["viewer_id"] == 202
    assert public["viewer_side"] == "opponent"
    assert state["actions"] == {}
    assert state["engine"]["player"]["hp"] == 130
    assert "skip" not in state["allowed_actions"]


@pytest.mark.asyncio
async def test_two_actions_resolve_once_and_publish_outcome(service):
    match = _match()
    now = datetime(2026, 8, 7, 12, 0, tzinfo=timezone.utc)
    await service.start_or_get(match, now=now)

    first = await service.submit_action(
        match, 101, ArenaPlayerAction.FAST_ATTACK, "a-1", now=now + timedelta(seconds=1)
    )
    assert first["phase_index"] == 0
    assert first["actions"]["101"]["action"] == "fast_attack"

    second = await service.submit_action(
        match, 202, ArenaPlayerAction.BLOCK, "b-1", now=now + timedelta(seconds=1)
    )
    assert second["phase_index"] == 1
    assert second["last_outcome"]["event_type"] == "phase_resolved"
    assert second["last_outcome"]["player_action"] == "fast_attack"
    assert second["last_outcome"]["opponent_action"] == "block"

    retried = await service.submit_action(
        match, 202, ArenaPlayerAction.BLOCK, "b-1", now=now + timedelta(seconds=1)
    )
    assert retried["phase_index"] == second["phase_index"]
    assert retried["last_outcome"] == second["last_outcome"]

    with pytest.raises(DuplicateAction):
        await service.submit_action(
            match, 101, ArenaPlayerAction.BLOCK, "b-1", now=now + timedelta(seconds=1)
        )
    assert any(channel == "arena:match:-700001:11" for channel, _ in service.redis.published)


@pytest.mark.asyncio
async def test_deadline_generates_skip_and_late_action_is_forbidden(service):
    match = _match()
    start = datetime(2026, 8, 7, 12, 0, tzinfo=timezone.utc)
    await service.start_or_get(match, now=start)

    with pytest.raises(SessionConflict, match="Фаза уже закрыта"):
        await service.submit_action(
            match, 101, ArenaPlayerAction.HEAVY_ATTACK, "late", now=start + timedelta(seconds=3)
        )
    state = await service.tick(match, now=start + timedelta(seconds=3))
    assert state["phase_index"] == 1
    assert state["last_outcome"]["player_action"] == "skip"
    assert state["last_outcome"]["opponent_action"] == "skip"


@pytest.mark.asyncio
async def test_disconnect_reconnect_and_technical_loss(service):
    match = _match()
    start = datetime(2026, 8, 7, 12, 0, tzinfo=timezone.utc)
    await service.start_or_get(match, now=start)

    disconnected = await service.disconnect(match, 101, now=start)
    assert disconnected["connections"]["101"]["connected"] is False
    assert disconnected["connections"]["101"]["deadline"] == "2026-08-07T12:00:15+00:00"

    reconnected = await service.reconnect(match, 101, now=start + timedelta(seconds=5))
    assert reconnected["connections"]["101"]["connected"] is True
    assert reconnected["phase_index"] == 0

    lost = await service.disconnect(match, 101, now=start)
    lost = await service.tick(match, now=start + timedelta(seconds=16))
    assert lost["terminal"] is True
    assert lost["result"] == "technical_loss"
    assert lost["winner_id"] == 202


@pytest.mark.asyncio
async def test_both_disconnected_request_refund(service):
    match = _match()
    start = datetime(2026, 8, 7, 12, 0, tzinfo=timezone.utc)
    await service.start_or_get(match, now=start)

    await service.disconnect(match, 101, now=start)
    state = await service.disconnect(match, 202, now=start)
    assert state["terminal"] is False
    state = await service.tick(match, now=start + timedelta(seconds=16))

    assert state["terminal"] is True
    assert state["result"] is None
    assert state["refund_required"] is True
    assert state["reason"] == "both_disconnected"


@pytest.mark.asyncio
async def test_forfeit_is_idempotent(service):
    match = _match()
    start = datetime(2026, 8, 7, 12, 0, tzinfo=timezone.utc)
    await service.start_or_get(match, now=start)

    first = await service.forfeit(match, 101, now=start + timedelta(seconds=1))
    second = await service.forfeit(match, 101, now=start + timedelta(seconds=1))

    assert first["result"] == "technical_loss"
    assert first["winner_id"] == 202
    assert second == {**first, "viewer_id": 101, "viewer_side": "player"}
