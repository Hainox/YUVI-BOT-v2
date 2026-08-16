from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi import HTTPException

from api.routes import arena as arena_routes
from api.routes.arena import _serialize_arena_match_result
from common.arena.config import ArenaConfig
from common.models.arena import ArenaMatch


def _match(**overrides) -> ArenaMatch:
    values = {
        "id": 7,
        "chat_id": -700001,
        "creator_id": 101,
        "creator_fighter": "tank",
        "creator_bet": 100,
        "creation_idempotency_key": "k",
        "opponent_id": 202,
        "opponent_fighter": "assassin",
        "opponent_bet": 150,
        "status": "finished",
        "match_result": "win",
        "result_reason": "knockout",
        "winner_id": 101,
        "loser_id": 202,
        "settlement_status": "paid",
        "payout_amount": 238,
        "created_at": datetime.now(timezone.utc),
        "expires_at": datetime.now(timezone.utc),
        "finished_at": datetime.now(timezone.utc),
    }
    values.update(overrides)
    return ArenaMatch(**values)


def test_result_is_viewer_scoped_and_calculates_win_summary() -> None:
    result = _serialize_arena_match_result(_match(), viewer_id=101)

    assert result["viewer_id"] == 101
    assert result["opponent_id"] == 202
    assert result["payout_amount"] == 238
    assert result["refund_amount"] == 0
    assert result["rating_delta"] == 25
    assert result["xp_gained"] == 150


def test_result_uses_shared_arena_config_for_rating_and_xp() -> None:
    original = arena_routes._ARENA_CONFIG
    arena_routes._ARENA_CONFIG = ArenaConfig(
        base_match_xp=7,
        win_bonus_xp=3,
        win_rating_delta=11,
        loss_rating_delta=13,
        technical_loss_rating_delta=17,
    )
    try:
        winner = _serialize_arena_match_result(_match(), viewer_id=101)
        loser = _serialize_arena_match_result(_match(), viewer_id=202)
    finally:
        arena_routes._ARENA_CONFIG = original

    assert winner["rating_delta"] == 11
    assert winner["xp_gained"] == 10
    assert loser["rating_delta"] == -13
    assert loser["xp_gained"] == 7


def test_result_scopes_opponent_view_without_leaking_creator_role() -> None:
    result = _serialize_arena_match_result(_match(), viewer_id=202)

    assert result["viewer_id"] == 202
    assert result["opponent_id"] == 101
    assert result["viewer_fighter"] == "assassin"
    assert result["rating_delta"] == -20
    assert result["xp_gained"] == 100
    assert result["payout_amount"] == 0


def test_result_rejects_non_participant() -> None:
    with pytest.raises(HTTPException) as error:
        _serialize_arena_match_result(_match(), viewer_id=303)

    assert error.value.status_code == 403


def test_draw_returns_refund_and_no_rating_delta() -> None:
    result = _serialize_arena_match_result(
        _match(
            match_result="draw",
            result_reason="time_limit",
            winner_id=None,
            loser_id=None,
            settlement_status="refunded",
            payout_amount=None,
        ),
        viewer_id=101,
    )

    assert result["refund_amount"] == 100
    assert result["rating_delta"] == 0
    assert result["xp_gained"] == 100


def test_both_disconnected_refund_is_not_reported_as_a_loss() -> None:
    result = _serialize_arena_match_result(
        _match(
            match_result=None,
            result_reason="both_disconnected",
            winner_id=None,
            loser_id=None,
            settlement_status="refunded",
            payout_amount=None,
        ),
        viewer_id=101,
    )

    assert result["result"] is None
    assert result["refund_amount"] == 100
    assert result["rating_delta"] == 0
    assert result["xp_gained"] == 0


def test_cancelled_match_without_opponent_keeps_empty_opponent_fields() -> None:
    result = _serialize_arena_match_result(
        _match(
            opponent_id=None,
            opponent_fighter=None,
            opponent_bet=None,
            status="cancelled",
            match_result=None,
            result_reason="creator_cancelled",
            winner_id=None,
            loser_id=None,
            settlement_status="refunded",
            payout_amount=None,
        ),
        viewer_id=101,
    )

    assert result["opponent_id"] is None
    assert result["opponent_name"] is None
    assert result["opponent_fighter"] is None
    assert result["opponent_bet"] == 0
    assert result["refund_amount"] == 100
