"""Arena Phase 2 foundation: persistence, settlement guards and audit.

Revision ID: 0019_arena_phase_2
Revises: 0018_exchange_stuck_alert
Create Date: 2026-08-07
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0019_arena_phase_2"
down_revision = "0018_exchange_stuck_alert"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "arena_profiles",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.BigInteger(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("rating", sa.Integer(), nullable=False, server_default="1000"),
        sa.Column("total_matches", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("wins", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("losses", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("draws", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("technical_wins", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("technical_losses", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("knockouts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint("user_id", name="uq_arena_profiles_user"),
        sa.CheckConstraint("rating >= 0", name="ck_arena_profiles_rating_nonneg"),
        sa.CheckConstraint("total_matches >= 0", name="ck_arena_profiles_matches_nonneg"),
        sa.CheckConstraint(
            "wins >= 0 AND losses >= 0 AND draws >= 0",
            name="ck_arena_profiles_results_nonneg",
        ),
    )
    op.create_index("ix_arena_profiles_rating", "arena_profiles", ["rating"])

    op.create_table(
        "arena_fighter_progress",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "profile_id",
            sa.BigInteger(),
            sa.ForeignKey("arena_profiles.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("fighter_type", sa.String(length=32), nullable=False),
        sa.Column("xp", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("level", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("cosmetics", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "profile_id", "fighter_type", name="uq_arena_fighter_progress_profile_fighter"
        ),
        sa.CheckConstraint("xp >= 0", name="ck_arena_fighter_progress_xp_nonneg"),
        sa.CheckConstraint(
            "level >= 1 AND level <= 50",
            name="ck_arena_fighter_progress_level_range",
        ),
    )
    op.create_index(
        "ix_arena_fighter_progress_profile",
        "arena_fighter_progress",
        ["profile_id"],
    )

    op.create_table(
        "arena_funds",
        sa.Column("chat_id", sa.BigInteger(), primary_key=True),
        sa.Column("balance", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint("balance >= 0", name="ck_arena_funds_balance_nonneg"),
    )

    # Matches are created before ledgers/events because those tables reference them.
    op.create_table(
        "arena_matches",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("creator_id", sa.BigInteger(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("creator_fighter", sa.String(length=32), nullable=False),
        sa.Column("creator_bet", sa.BigInteger(), nullable=False),
        sa.Column("creation_idempotency_key", sa.String(length=120), nullable=False),
        sa.Column("opponent_id", sa.BigInteger(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("opponent_fighter", sa.String(length=32), nullable=True),
        sa.Column("opponent_bet", sa.BigInteger(), nullable=True),
        sa.Column("creator_confirmed", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("opponent_confirmed", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="waiting"),
        sa.Column("match_result", sa.String(length=32), nullable=True),
        sa.Column("result_reason", sa.String(length=32), nullable=True),
        sa.Column("winner_id", sa.BigInteger(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("loser_id", sa.BigInteger(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("settlement_status", sa.String(length=16), nullable=False, server_default="pending"),
        sa.Column("settlement_idempotency_key", sa.String(length=120), nullable=True),
        sa.Column("payout_amount", sa.BigInteger(), nullable=True),
        sa.Column("fee_chat", sa.BigInteger(), nullable=True),
        sa.Column("fee_fund", sa.BigInteger(), nullable=True),
        sa.Column("replay_data", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("engine_version", sa.String(length=32), nullable=True),
        sa.Column("phase_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("accept_deadline", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint(
            "creator_id",
            "creation_idempotency_key",
            name="uq_arena_matches_creator_idempotency",
        ),
        sa.UniqueConstraint(
            "settlement_idempotency_key",
            name="uq_arena_matches_settlement_idempotency",
        ),
        sa.CheckConstraint("creator_bet >= 100", name="ck_arena_matches_creator_bet_min"),
        sa.CheckConstraint(
            "opponent_bet IS NULL OR opponent_bet >= 100",
            name="ck_arena_matches_opponent_bet_min",
        ),
        sa.CheckConstraint(
            "creator_bet >= 0 AND (opponent_bet IS NULL OR opponent_bet >= 0)",
            name="ck_arena_matches_amounts_nonnegative",
        ),
        sa.CheckConstraint(
            "opponent_id IS NULL OR opponent_id <> creator_id",
            name="ck_arena_matches_not_self",
        ),
        sa.CheckConstraint(
            "(opponent_id IS NULL AND opponent_fighter IS NULL AND opponent_bet IS NULL) "
            "OR (opponent_id IS NOT NULL AND opponent_fighter IS NOT NULL AND opponent_bet IS NOT NULL)",
            name="ck_arena_matches_opponent_pair",
        ),
        sa.CheckConstraint(
            "status IN ('waiting', 'accepting', 'active', 'finished', 'cancelled', 'refunded')",
            name="ck_arena_matches_status",
        ),
        sa.CheckConstraint(
            "match_result IS NULL OR match_result IN ('win', 'draw', 'technical_loss')",
            name="ck_arena_matches_result",
        ),
        sa.CheckConstraint(
            "settlement_status IN ('pending', 'paid', 'refunded', 'not_required')",
            name="ck_arena_matches_settlement_status",
        ),
    )
    op.create_index("ix_arena_matches_chat_status", "arena_matches", ["chat_id", "status"])
    op.create_index("ix_arena_matches_creator", "arena_matches", ["creator_id", "created_at"])

    op.create_table(
        "arena_fund_ledger",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("amount", sa.BigInteger(), nullable=False),
        sa.Column("balance_after", sa.BigInteger(), nullable=False),
        sa.Column("kind", sa.String(length=40), nullable=False),
        sa.Column("idempotency_key", sa.String(length=120), nullable=False),
        sa.Column(
            "match_id",
            sa.BigInteger(),
            sa.ForeignKey("arena_matches.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint(
            "chat_id", "idempotency_key", name="uq_arena_fund_ledger_idempotency"
        ),
        sa.CheckConstraint(
            "balance_after >= 0",
            name="ck_arena_fund_ledger_balance_after_nonneg",
        ),
    )
    op.create_index("ix_arena_fund_ledger_chat_created", "arena_fund_ledger", ["chat_id", "created_at"])

    op.create_table(
        "arena_match_events",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "match_id",
            sa.BigInteger(),
            sa.ForeignKey("arena_matches.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("event_key", sa.String(length=120), nullable=False),
        sa.Column("event_type", sa.String(length=40), nullable=False),
        sa.Column("phase_index", sa.Integer(), nullable=False),
        sa.Column("actor_user_id", sa.BigInteger(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint(
            "match_id", "sequence", name="uq_arena_match_events_match_sequence"
        ),
        sa.UniqueConstraint("match_id", "event_key", name="uq_arena_match_events_match_key"),
        sa.CheckConstraint("sequence >= 0", name="ck_arena_match_events_sequence_nonneg"),
        sa.CheckConstraint("phase_index >= 0", name="ck_arena_match_events_phase_nonneg"),
    )
    op.create_index("ix_arena_match_events_match_phase", "arena_match_events", ["match_id", "phase_index"])

    op.create_table(
        "arena_leaderboard_snapshots",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("period_start", sa.Date(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("rank_position", sa.Integer(), nullable=False),
        sa.Column("rating", sa.Integer(), nullable=False),
        sa.Column("wins", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("matches", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("reward_amount", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint(
            "period_start", "user_id", name="uq_arena_leaderboard_snapshot_period_user"
        ),
        sa.CheckConstraint(
            "rank_position >= 1",
            name="ck_arena_leaderboard_snapshot_rank_positive",
        ),
    )
    op.create_index(
        "ix_arena_leaderboard_snapshots_period_rank",
        "arena_leaderboard_snapshots",
        ["period_start", "rank_position"],
    )

    op.create_table(
        "arena_daily_awards",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("award_date", sa.Date(), nullable=False),
        sa.Column("award_key", sa.String(length=40), nullable=False),
        sa.Column("user_id", sa.BigInteger(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("reward_amount", sa.BigInteger(), nullable=False),
        sa.Column("fund_ledger_key", sa.String(length=120), nullable=False),
        sa.Column("details", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("awarded_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint(
            "chat_id",
            "award_date",
            "award_key",
            "user_id",
            name="uq_arena_daily_awards_recipient",
        ),
        sa.UniqueConstraint("fund_ledger_key", name="uq_arena_daily_awards_fund_key"),
        sa.CheckConstraint(
            "reward_amount >= 0",
            name="ck_arena_daily_awards_reward_nonneg",
        ),
    )

    op.create_table(
        "arena_weekly_awards",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("week_start", sa.Date(), nullable=False),
        sa.Column("award_key", sa.String(length=40), nullable=False),
        sa.Column("user_id", sa.BigInteger(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("reward_amount", sa.BigInteger(), nullable=False),
        sa.Column("fund_ledger_key", sa.String(length=120), nullable=False),
        sa.Column("details", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("awarded_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint(
            "chat_id",
            "week_start",
            "award_key",
            "user_id",
            name="uq_arena_weekly_awards_recipient",
        ),
        sa.UniqueConstraint("fund_ledger_key", name="uq_arena_weekly_awards_fund_key"),
        sa.CheckConstraint(
            "reward_amount >= 0",
            name="ck_arena_weekly_awards_reward_nonneg",
        ),
    )

    op.create_table(
        "arena_admin_audit",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("admin_id", sa.BigInteger(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("operation", sa.String(length=64), nullable=False),
        sa.Column("idempotency_key", sa.String(length=120), nullable=False),
        sa.Column(
            "match_id",
            sa.BigInteger(),
            sa.ForeignKey("arena_matches.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("amount", sa.BigInteger(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("result", sa.String(length=32), nullable=True),
        sa.Column("details", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint(
            "admin_id", "idempotency_key", name="uq_arena_admin_audit_idempotency"
        ),
    )
    op.create_index("ix_arena_admin_audit_match_created", "arena_admin_audit", ["match_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_arena_admin_audit_match_created", table_name="arena_admin_audit")
    op.drop_table("arena_admin_audit")

    op.drop_table("arena_weekly_awards")
    op.drop_table("arena_daily_awards")

    op.drop_index(
        "ix_arena_leaderboard_snapshots_period_rank",
        table_name="arena_leaderboard_snapshots",
    )
    op.drop_table("arena_leaderboard_snapshots")

    op.drop_index("ix_arena_match_events_match_phase", table_name="arena_match_events")
    op.drop_table("arena_match_events")

    op.drop_index("ix_arena_fund_ledger_chat_created", table_name="arena_fund_ledger")
    op.drop_table("arena_fund_ledger")

    op.drop_index("ix_arena_matches_creator", table_name="arena_matches")
    op.drop_index("ix_arena_matches_chat_status", table_name="arena_matches")
    op.drop_table("arena_matches")

    op.drop_table("arena_funds")

    op.drop_index("ix_arena_fighter_progress_profile", table_name="arena_fighter_progress")
    op.drop_table("arena_fighter_progress")

    op.drop_index("ix_arena_profiles_rating", table_name="arena_profiles")
    op.drop_table("arena_profiles")
