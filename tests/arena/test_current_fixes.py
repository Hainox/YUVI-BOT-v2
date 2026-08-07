from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import Mock

import pytest

from bot.services import arena_service
from bot.services.arena_session_service import ArenaSessionService
from bot.services import arena_runtime_worker
from common.arena.schemas import FighterType


class _Lock:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None


class _Redis:
    def __init__(self):
        self.values = {}
        self.published = []

    async def get(self, key):
        return self.values.get(key)

    async def set(self, key, value, **kwargs):
        self.values[key] = value
        return True

    async def publish(self, channel, payload):
        self.published.append((channel, payload))
        return 1

    def lock(self, *args, **kwargs):
        return _Lock()


def _active_match():
    return SimpleNamespace(
        id=41,
        chat_id=-700001,
        creator_id=101,
        creator_fighter=FighterType.TANK.value,
        opponent_id=202,
        opponent_fighter=FighterType.ASSASSIN.value,
        status="active",
        replay_data=None,
        phase_count=0,
    )


def _waiting_match():
    return SimpleNamespace(
        id=None,
        chat_id=-700001,
        creator_id=101,
        creator_fighter=FighterType.TANK.value,
        creator_bet=100,
        creation_idempotency_key="create-rollback",
        status="waiting",
        settlement_status="pending",
        created_at=None,
        expires_at=None,
    )


@pytest.mark.asyncio
async def test_create_match_rolls_back_when_match_persistence_fails(monkeypatch):
    session = AsyncMock()
    session.add = Mock()
    session.commit = AsyncMock(side_effect=RuntimeError("database write failed"))
    session.rollback = AsyncMock()
    monkeypatch.setattr(arena_service.economy_service, "debit", AsyncMock(return_value=True))

    with pytest.raises(RuntimeError, match="database write failed"):
        await arena_service.create_match(
            session,
            -700001,
            101,
            FighterType.TANK,
            100,
            "create-rollback",
        )

    session.rollback.assert_awaited_once()


@pytest.mark.asyncio
async def test_accept_match_rolls_back_when_match_update_commit_fails(monkeypatch):
    session = AsyncMock()
    session.commit = AsyncMock(side_effect=RuntimeError("database write failed"))
    session.rollback = AsyncMock()
    match = SimpleNamespace(
        id=42,
        chat_id=-700001,
        creator_id=101,
        status="waiting",
        expires_at=datetime(2026, 8, 7, 13, 0, tzinfo=timezone.utc),
    )
    monkeypatch.setattr(arena_service, "_get_match_for_update", AsyncMock(return_value=match))
    monkeypatch.setattr(arena_service.economy_service, "debit", AsyncMock(return_value=True))

    with pytest.raises(RuntimeError, match="database write failed"):
        await arena_service.accept_match(
            session,
            -700001,
            42,
            202,
            FighterType.ASSASSIN,
            100,
            "accept-rollback",
            now=datetime(2026, 8, 7, 12, 0, tzinfo=timezone.utc),
        )

    session.rollback.assert_awaited_once()


@pytest.mark.asyncio
async def test_tick_publishes_every_caught_up_phase():
    redis = _Redis()
    service = ArenaSessionService(redis)
    match = _active_match()
    start = datetime(2026, 8, 7, 12, 0, tzinfo=timezone.utc)
    await service.start_or_get(match, now=start)

    await service.tick(match, now=start + timedelta(seconds=10))

    phase_events = [payload for channel, payload in redis.published if '"event_type": "phase_resolved"' in payload]
    assert len(phase_events) == 3


@pytest.mark.asyncio
async def test_worker_shutdown_closes_owned_client():
    class _Client:
        def __init__(self):
            self.closed = False

        async def aclose(self):
            self.closed = True

    client = _Client()
    arena_runtime_worker._runtime_client = client
    await arena_runtime_worker.shutdown_runtime_tick()

    assert client.closed is True
    assert arena_runtime_worker._runtime_client is None


@pytest.mark.asyncio
async def test_non_phase_event_does_not_publish_pending_actions():
    redis = _Redis()
    service = ArenaSessionService(redis)
    match = _active_match()
    start = datetime(2026, 8, 7, 12, 0, tzinfo=timezone.utc)
    await service.start_or_get(match, now=start)
    await service.submit_action(match, 101, "block", "safe-action", now=start + timedelta(seconds=1))

    await service.disconnect(match, 101, now=start + timedelta(seconds=1))
    disconnect_payload = redis.published[-1][1]
    assert '"event_type": "player_disconnected"' in disconnect_payload
    assert '"actions"' not in disconnect_payload
    assert '"processed_actions"' not in disconnect_payload

    await service.reconnect(match, 101, now=start + timedelta(seconds=2))
    reconnect_payload = redis.published[-1][1]
    assert '"event_type": "player_reconnected"' in reconnect_payload
    assert '"actions"' not in reconnect_payload
    assert '"processed_actions"' not in reconnect_payload

    await service.forfeit(match, 101, now=start + timedelta(seconds=2))
    terminal_payload = redis.published[-1][1]
    assert '"event_type": "match_terminal"' in terminal_payload
    assert '"actions"' not in terminal_payload
    assert '"processed_actions"' not in terminal_payload
