from __future__ import annotations

from types import SimpleNamespace

from bot.services.arena_replay_service import HEIGHT
from bot.services.arena_replay_service import WIDTH
from bot.services.arena_replay_service import _frame
from bot.services.arena_replay_service import event_payload


def test_replay_frame_is_fixed_size_and_deterministic() -> None:
    event = {
        "sequence": 1,
        "phase_index": 2,
        "payload": {
            "player_hp": 80,
            "opponent_hp": 42,
            "player_damage": 12,
            "opponent_damage": 4,
            "player_special": True,
        },
    }
    match = SimpleNamespace(id=7)
    first = _frame(event, match, 0)
    second = _frame(event, match, 0)

    assert first == second
    assert len(first) == WIDTH * HEIGHT * 3
    assert len(set(first)) > 3


def test_event_payload_does_not_expose_orm_object() -> None:
    event = SimpleNamespace(
        sequence=4,
        phase_index=3,
        event_type="phase_resolved",
        payload={"player_hp": 90},
        secret="must not leak",
    )

    payload = event_payload(event)

    assert payload == {
        "sequence": 4,
        "phase_index": 3,
        "event_type": "phase_resolved",
        "payload": {"player_hp": 90},
    }
    assert "secret" not in payload
