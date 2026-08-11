from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi import HTTPException

import api.routes.arena as arena_routes
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


def test_rating_and_xp_are_read_from_config_not_hardcoded(monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression for a real bug found in review: `_serialize_arena_match_result`
    used to hardcode the 25/-20/-30/150/100 rating/XP literals instead of
    reading `ArenaConfig`, so a legitimate config-only rebalance
    (FIGHTING_SPEC.md 15: "изменение правил только через код/конфигурацию")
    would silently desync the result/history screens from the numbers
    `arena_service.settle_match` actually applied. Swapping in a config with
    different values must change the route's output, proving it's read live
    rather than duplicated as a literal."""
    monkeypatch.setattr(
        arena_routes,
        "_ARENA_CONFIG",
        ArenaConfig(
            win_rating_delta=99,
            loss_rating_delta=88,
            technical_loss_rating_delta=77,
            base_match_xp=66,
            win_bonus_xp=11,
        ),
    )

    winner_result = _serialize_arena_match_result(_match(), viewer_id=101)
    assert winner_result["rating_delta"] == 99
    assert winner_result["xp_gained"] == 77  # base_match_xp + win_bonus_xp

    loser_result = _serialize_arena_match_result(_match(), viewer_id=202)
    assert loser_result["rating_delta"] == -88
    assert loser_result["xp_gained"] == 66

    technical_loser_result = _serialize_arena_match_result(
        _match(match_result="technical_loss", result_reason="disconnect_timeout"),
        viewer_id=202,
    )
    assert technical_loser_result["rating_delta"] == -77
    assert technical_loser_result["xp_gained"] == 0


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
