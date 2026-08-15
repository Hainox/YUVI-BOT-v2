from __future__ import annotations

import json

import os

os.environ.setdefault("BOT_TOKEN", "test-token")
os.environ.setdefault("CHAT_ID", "-700001")
os.environ.setdefault("OWNER_ID", "1")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost:5432/test")

import pytest

from api.routes.arena_runtime import _map_session_error
from api.routes.arena_runtime import _arena_event_stream
from bot.services.arena_session_service import DuplicateAction
from bot.services.arena_session_service import InvalidAction
from bot.services.arena_session_service import SessionNotFound


class _PubSub:
    def __init__(self):
        self.messages = [{"data": json.dumps({"event_type": "phase_resolved"})}, None]

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def subscribe(self, channel):
        self.channel = channel

    async def get_message(self, **kwargs):
        return self.messages.pop(0)


class _Redis:
    def pubsub(self):
        return _PubSub()


@pytest.mark.asyncio
async def test_arena_event_stream_uses_only_match_channel():
    disconnected_calls = 0

    async def disconnected():
        nonlocal disconnected_calls
        disconnected_calls += 1
        return disconnected_calls > 2

    chunks = [chunk async for chunk in _arena_event_stream(_Redis(), "arena:match:-7:11", disconnected)]

    assert chunks == ['data: {"event_type": "phase_resolved"}\n\n', ": ping\n\n"]


def test_runtime_errors_map_to_stable_http_codes():
    assert _map_session_error(SessionNotFound("missing")).status_code == 404
    assert _map_session_error(DuplicateAction("duplicate")).status_code == 409
    assert _map_session_error(InvalidAction("bad")).status_code == 422
