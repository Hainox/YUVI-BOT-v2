"""Tests for `api/routes/arena_training.py` — same lightweight, mocked-
`Request`/direct-handler-call style as `tests/arena/test_api_security.py`/
`test_runtime_contract.py` (this subsystem's established API-test
convention), rather than a full ASGI/HTTP client. Uses a real
`ArenaTrainingService` backed by an in-memory FakeRedis (via
`request.app.state.redis`), so these exercise the real service through the
route layer, not a mock of it.
"""

from __future__ import annotations

import os

os.environ.setdefault("BOT_TOKEN", "test-token")
os.environ.setdefault("CHAT_ID", "-700001")
os.environ.setdefault("OWNER_ID", "1")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost:5432/test")

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from api.deps import AuthContext
from api.routes import arena_training as training_route
from bot.services import arena_training_service


class _Lock:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None


class FakeRedis:
    def __init__(self):
        self.data: dict[str, str] = {}

    async def get(self, key: str):
        return self.data.get(key)

    async def set(self, key: str, value: str, **kwargs):
        self.data[key] = value
        return True

    def lock(self, key: str, **kwargs):
        return _Lock()


class _State:
    def __init__(self, redis=None):
        self.redis = redis


class _App:
    def __init__(self, redis=None):
        self.state = _State(redis)


def _request(redis=None) -> Request:
    return Request({"type": "http", "app": _App(redis), "headers": [], "query_string": b""})


AUTH = AuthContext(user_id=501, chat_id=-800001, status="member")


@pytest.mark.asyncio
async def test_start_training_returns_fresh_session():
    request = _request(FakeRedis())
    body = training_route.StartTrainingBody(fighter="tank", opponent_fighter="assassin")

    state = await training_route.post_start_training(body, request, AUTH)

    assert state["fighters"] == {"player": "tank", "opponent": "assassin"}
    assert state["terminal"] is False


@pytest.mark.asyncio
async def test_get_state_without_session_returns_404():
    request = _request(FakeRedis())
    with pytest.raises(HTTPException) as exc_info:
        await training_route.get_training_state(request, AUTH)
    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_start_then_get_state_round_trips():
    redis = FakeRedis()
    start_body = training_route.StartTrainingBody(fighter="berserker", opponent_fighter="tactician")
    await training_route.post_start_training(start_body, _request(redis), AUTH)

    state = await training_route.get_training_state(_request(redis), AUTH)
    assert state["fighters"] == {"player": "berserker", "opponent": "tactician"}


@pytest.mark.asyncio
async def test_submit_action_advances_phase():
    redis = FakeRedis()
    start_body = training_route.StartTrainingBody(fighter="tank", opponent_fighter="assassin")
    await training_route.post_start_training(start_body, _request(redis), AUTH)

    action_body = training_route.TrainingActionBody(action="fast_attack")
    state = await training_route.post_training_action(action_body, _request(redis), AUTH)
    assert state["phase_index"] == 1


@pytest.mark.asyncio
async def test_submit_invalid_action_returns_422():
    redis = FakeRedis()
    start_body = training_route.StartTrainingBody(fighter="tank", opponent_fighter="assassin")
    await training_route.post_start_training(start_body, _request(redis), AUTH)

    action_body = training_route.TrainingActionBody(action="not_a_real_action")
    with pytest.raises(HTTPException) as exc_info:
        await training_route.post_training_action(action_body, _request(redis), AUTH)
    assert exc_info.value.status_code == 422


@pytest.mark.asyncio
async def test_pause_then_action_returns_409():
    redis = FakeRedis()
    start_body = training_route.StartTrainingBody(fighter="tank", opponent_fighter="assassin")
    await training_route.post_start_training(start_body, _request(redis), AUTH)
    await training_route.post_training_pause(
        training_route.SetPausedBody(paused=True), _request(redis), AUTH
    )

    action_body = training_route.TrainingActionBody(action="fast_attack")
    with pytest.raises(HTTPException) as exc_info:
        await training_route.post_training_action(action_body, _request(redis), AUTH)
    assert exc_info.value.status_code == 409


@pytest.mark.asyncio
async def test_no_redis_configured_returns_503():
    request = _request(None)
    body = training_route.StartTrainingBody(fighter="tank")
    with pytest.raises(HTTPException) as exc_info:
        await training_route.post_start_training(body, request, AUTH)
    assert exc_info.value.status_code == 503


@pytest.mark.asyncio
async def test_identity_comes_only_from_auth_context_not_body():
    """IDOR contract (same as every other route in this repo): the training
    session key is derived from `auth.user_id`/`auth.chat_id`, and neither
    `StartTrainingBody` nor `TrainingActionBody` has any user/chat field at
    all — confirmed structurally (Pydantic model fields), not just by
    convention."""
    assert set(training_route.StartTrainingBody.model_fields) == {"fighter", "opponent_fighter"}
    assert set(training_route.TrainingActionBody.model_fields) == {"action"}
    assert set(training_route.SetPausedBody.model_fields) == {"paused"}


def test_error_mapping_status_codes():
    assert (
        training_route._map_error(arena_training_service.TrainingSessionNotFound("x")).status_code
        == 404
    )
    assert (
        training_route._map_error(arena_training_service.TrainingActionNotAvailable("x")).status_code
        == 422
    )
    assert (
        training_route._map_error(arena_training_service.TrainingSessionPaused("x")).status_code
        == 409
    )
    assert (
        training_route._map_error(arena_training_service.ArenaTrainingError("x")).status_code == 400
    )
