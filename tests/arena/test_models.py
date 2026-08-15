from __future__ import annotations

from datetime import date

from sqlalchemy import DateTime
from sqlalchemy import UniqueConstraint

from common.db.base import Base
from common.models.arena import ArenaAdminAudit
from common.models.arena import ArenaDailyAward
from common.models.arena import ArenaFighterProgress
from common.models.arena import ArenaFund
from common.models.arena import ArenaFundLedger
from common.models.arena import ArenaLeaderboardSnapshot
from common.models.arena import ArenaMatch
from common.models.arena import ArenaMatchEvent
from common.models.arena import ArenaProfile
from common.models.arena import ArenaWeeklyAward


_EXPECTED_TABLES = {
    "arena_profiles",
    "arena_fighter_progress",
    "arena_funds",
    "arena_fund_ledger",
    "arena_matches",
    "arena_match_events",
    "arena_leaderboard_snapshots",
    "arena_daily_awards",
    "arena_weekly_awards",
    "arena_admin_audit",
}


def _constraint_names(table) -> set[str]:
    return {constraint.name for constraint in table.constraints if constraint.name}


def test_arena_models_are_registered_in_metadata() -> None:
    assert _EXPECTED_TABLES <= set(Base.metadata.tables)


def test_profiles_and_fighter_progress_have_expected_identity_constraints() -> None:
    assert "uq_arena_profiles_user" in _constraint_names(ArenaProfile.__table__)
    assert "uq_arena_fighter_progress_profile_fighter" in _constraint_names(
        ArenaFighterProgress.__table__
    )
    assert ArenaFighterProgress.__table__.c.profile_id.foreign_keys
    assert ArenaFighterProgress.__table__.c.fighter_type.type.length == 32


def test_match_has_financial_and_idempotency_guards() -> None:
    names = _constraint_names(ArenaMatch.__table__)
    assert {
        "uq_arena_matches_creator_idempotency",
        "ck_arena_matches_creator_bet_min",
        "ck_arena_matches_opponent_bet_min",
        "ck_arena_matches_not_self",
        "ck_arena_matches_amounts_nonnegative",
    } <= names
    assert ArenaMatch.__table__.c.creation_idempotency_key.nullable is False
    assert ArenaMatch.__table__.c.creator_bet.nullable is False
    assert ArenaMatch.__table__.c.opponent_bet.nullable is True
    assert ArenaMatch.__table__.c.creator_confirmed.nullable is False
    assert ArenaMatch.__table__.c.opponent_confirmed.nullable is False
    assert ArenaMatch.__table__.c.replay_data.type.__class__.__name__ == "JSONB"


def test_events_are_ordered_and_idempotent_per_match() -> None:
    names = _constraint_names(ArenaMatchEvent.__table__)
    assert {
        "uq_arena_match_events_match_sequence",
        "uq_arena_match_events_match_key",
    } <= names
    assert ArenaMatchEvent.__table__.c.match_id.foreign_keys
    assert ArenaMatchEvent.__table__.c.payload.nullable is False


def test_money_and_timestamps_use_safe_types() -> None:
    for model in (ArenaFund, ArenaFundLedger, ArenaMatch, ArenaAdminAudit):
        for column in model.__table__.columns:
            if column.name.endswith("_at"):
                assert isinstance(column.type, DateTime)
                assert column.type.timezone is True

    assert ArenaFund.__table__.c.balance.type.python_type is int
    assert ArenaFundLedger.__table__.c.amount.type.python_type is int
    assert ArenaDailyAward.__table__.c.award_date.type.python_type is date
    assert any(
        isinstance(constraint, UniqueConstraint)
        and constraint.name == "uq_arena_daily_awards_recipient"
        for constraint in ArenaDailyAward.__table__.constraints
    )


def test_award_keys_prevent_duplicate_grants() -> None:
    assert "uq_arena_daily_awards_recipient" in _constraint_names(ArenaDailyAward.__table__)
    assert "uq_arena_daily_awards_fund_key" in _constraint_names(ArenaDailyAward.__table__)
    assert "uq_arena_weekly_awards_recipient" in _constraint_names(ArenaWeeklyAward.__table__)
    assert "uq_arena_weekly_awards_fund_key" in _constraint_names(ArenaWeeklyAward.__table__)
    assert "uq_arena_leaderboard_snapshot_period_user" in _constraint_names(
        ArenaLeaderboardSnapshot.__table__
    )


def test_admin_audit_requires_an_actor_and_reason() -> None:
    table = ArenaAdminAudit.__table__
    assert table.c.admin_id.nullable is False
    assert table.c.reason.nullable is False
    assert table.c.match_id.nullable is True
    assert table.c.details.type.__class__.__name__ == "JSONB"
