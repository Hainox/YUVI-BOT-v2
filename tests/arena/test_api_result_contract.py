from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi import HTTPException

from api.routes.arena import _serialize_arena_match_result
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
