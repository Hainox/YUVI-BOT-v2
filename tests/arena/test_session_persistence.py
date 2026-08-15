from __future__ import annotations

from bot.services.arena_session_service import ArenaSessionService
from bot.services.arena_session_service import _engine_from_dict
from bot.services.arena_session_service import _engine_to_dict
from common.arena.engine import ArenaEngine
from common.arena.schemas import FighterType


def test_engine_round_trip_preserves_trap_phase():
    state = ArenaEngine().start(FighterType.TANK, FighterType.TACTICIAN)
    state.opponent.trap_phases = 1
    restored = _engine_from_dict(_engine_to_dict(state))
    assert restored.opponent.trap_phases == 1


def test_public_state_hides_other_player_action():
    state = {
        "players": {"player": 101, "opponent": 202},
        "actions": {"101": {"action": "block"}, "202": {"action": "special"}},
        "processed_actions": {"a": {"user_id": 101}, "b": {"user_id": 202}},
    }
    public = ArenaSessionService.public_state(state, 101)
    assert list(public["actions"]) == ["101"]
    assert list(public["processed_actions"]) == ["a"]
