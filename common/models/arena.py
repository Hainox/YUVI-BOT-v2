from __future__ import annotations

from datetime import date
from datetime import datetime

from sqlalchemy import BigInteger
from sqlalchemy import Boolean
from sqlalchemy import CheckConstraint
from sqlalchemy import Date
from sqlalchemy import DateTime
from sqlalchemy import ForeignKey
from sqlalchemy import Integer
from sqlalchemy import String
from sqlalchemy import Text
from sqlalchemy import false
from sqlalchemy import UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.sql import func
from sqlalchemy.orm import Mapped
from sqlalchemy.orm import mapped_column

from common.db.base import Base


class ArenaProfile(Base):
    """Global Arena rating and aggregate PvP statistics for one user."""

    __tablename__ = "arena_profiles"
    __table_args__ = (
        UniqueConstraint("user_id", name="uq_arena_profiles_user"),
        CheckConstraint("rating >= 0", name="ck_arena_profiles_rating_nonneg"),
        CheckConstraint("total_matches >= 0", name="ck_arena_profiles_matches_nonneg"),
        CheckConstraint("wins >= 0 AND losses >= 0 AND draws >= 0", name="ck_arena_profiles_results_nonneg"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    rating: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1000")
    total_matches: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    wins: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    losses: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    draws: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    technical_wins: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    technical_losses: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    knockouts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class ArenaFighterProgress(Base):
    """XP and cosmetic progression for a concrete fighter owned by a user."""

    __tablename__ = "arena_fighter_progress"
    __table_args__ = (
        UniqueConstraint(
            "profile_id", "fighter_type", name="uq_arena_fighter_progress_profile_fighter"
        ),
        CheckConstraint("xp >= 0", name="ck_arena_fighter_progress_xp_nonneg"),
        CheckConstraint("level >= 1 AND level <= 50", name="ck_arena_fighter_progress_level_range"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    profile_id: Mapped[int] = mapped_column(
        ForeignKey("arena_profiles.id", ondelete="CASCADE"), nullable=False
    )
    fighter_type: Mapped[str] = mapped_column(String(32), nullable=False)
    xp: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    level: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    cosmetics: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class ArenaFund(Base):
    """Per-chat Arena fund used for fees and awards."""

    __tablename__ = "arena_funds"
    __table_args__ = (
        CheckConstraint("balance >= 0", name="ck_arena_funds_balance_nonneg"),
    )

    chat_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    balance: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class ArenaFundLedger(Base):
    """Append-only signed ledger for every Arena fund credit/debit."""

    __tablename__ = "arena_fund_ledger"
    __table_args__ = (
        UniqueConstraint("chat_id", "idempotency_key", name="uq_arena_fund_ledger_idempotency"),
        CheckConstraint("balance_after >= 0", name="ck_arena_fund_ledger_balance_after_nonneg"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    amount: Mapped[int] = mapped_column(BigInteger, nullable=False)
    balance_after: Mapped[int] = mapped_column(BigInteger, nullable=False)
    kind: Mapped[str] = mapped_column(String(40), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(120), nullable=False)
    match_id: Mapped[int | None] = mapped_column(
        ForeignKey("arena_matches.id", ondelete="SET NULL"), nullable=True
    )
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class ArenaMatch(Base):
    """Lobby, active combat and settlement record for one Arena match."""

    __tablename__ = "arena_matches"
    __table_args__ = (
        UniqueConstraint(
            "creator_id", "creation_idempotency_key", name="uq_arena_matches_creator_idempotency"
        ),
        UniqueConstraint(
            "settlement_idempotency_key", name="uq_arena_matches_settlement_idempotency"
        ),
        CheckConstraint("creator_bet >= 100", name="ck_arena_matches_creator_bet_min"),
        CheckConstraint(
            "opponent_bet IS NULL OR opponent_bet >= 100",
            name="ck_arena_matches_opponent_bet_min",
        ),
        CheckConstraint(
            "creator_bet >= 0 AND (opponent_bet IS NULL OR opponent_bet >= 0)",
            name="ck_arena_matches_amounts_nonnegative",
        ),
        CheckConstraint(
            "opponent_id IS NULL OR opponent_id <> creator_id",
            name="ck_arena_matches_not_self",
        ),
        CheckConstraint(
            "(opponent_id IS NULL AND opponent_fighter IS NULL AND opponent_bet IS NULL) "
            "OR (opponent_id IS NOT NULL AND opponent_fighter IS NOT NULL AND opponent_bet IS NOT NULL)",
            name="ck_arena_matches_opponent_pair",
        ),
        CheckConstraint(
            "status IN ('waiting', 'accepting', 'active', 'finished', 'cancelled', 'refunded')",
            name="ck_arena_matches_status",
        ),
        CheckConstraint(
            "match_result IS NULL OR match_result IN ('win', 'draw', 'technical_loss')",
            name="ck_arena_matches_result",
        ),
        CheckConstraint(
            "settlement_status IN ('pending', 'paid', 'refunded', 'not_required')",
            name="ck_arena_matches_settlement_status",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    creator_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    creator_fighter: Mapped[str] = mapped_column(String(32), nullable=False)
    creator_bet: Mapped[int] = mapped_column(BigInteger, nullable=False)
    creation_idempotency_key: Mapped[str] = mapped_column(String(120), nullable=False)
    opponent_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    opponent_fighter: Mapped[str | None] = mapped_column(String(32), nullable=True)
    opponent_bet: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    creator_confirmed: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=false())
    opponent_confirmed: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=false())
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="waiting")
    match_result: Mapped[str | None] = mapped_column(String(32), nullable=True)
    result_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)
    winner_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    loser_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    settlement_status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="pending"
    )
    settlement_idempotency_key: Mapped[str | None] = mapped_column(String(120), nullable=True)
    payout_amount: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    fee_chat: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    fee_fund: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    replay_data: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    engine_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    phase_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    accept_deadline: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ArenaMatchEvent(Base):
    """Ordered, idempotent authoritative event in a match replay."""

    __tablename__ = "arena_match_events"
    __table_args__ = (
        UniqueConstraint(
            "match_id", "sequence", name="uq_arena_match_events_match_sequence"
        ),
        UniqueConstraint("match_id", "event_key", name="uq_arena_match_events_match_key"),
        CheckConstraint("sequence >= 0", name="ck_arena_match_events_sequence_nonneg"),
        CheckConstraint("phase_index >= 0", name="ck_arena_match_events_phase_nonneg"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    match_id: Mapped[int] = mapped_column(
        ForeignKey("arena_matches.id", ondelete="CASCADE"), nullable=False
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    event_key: Mapped[str] = mapped_column(String(120), nullable=False)
    event_type: Mapped[str] = mapped_column(String(40), nullable=False)
    phase_index: Mapped[int] = mapped_column(Integer, nullable=False)
    actor_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class ArenaLeaderboardSnapshot(Base):
    """Immutable user/rank snapshot for a weekly leaderboard period."""

    __tablename__ = "arena_leaderboard_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "period_start", "user_id", name="uq_arena_leaderboard_snapshot_period_user"
        ),
        CheckConstraint("rank_position >= 1", name="ck_arena_leaderboard_snapshot_rank_positive"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    period_start: Mapped[date] = mapped_column(Date, nullable=False)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    rank_position: Mapped[int] = mapped_column(Integer, nullable=False)
    rating: Mapped[int] = mapped_column(Integer, nullable=False)
    wins: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    matches: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    reward_amount: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class ArenaDailyAward(Base):
    """Idempotent daily award nomination/grant."""

    __tablename__ = "arena_daily_awards"
    __table_args__ = (
        UniqueConstraint(
            "chat_id",
            "award_date",
            "award_key",
            "user_id",
            name="uq_arena_daily_awards_recipient",
        ),
        UniqueConstraint(
            "chat_id", "award_date", "award_key", name="uq_arena_daily_awards_category"
        ),
        CheckConstraint("reward_amount >= 0", name="ck_arena_daily_awards_reward_nonneg"),
        CheckConstraint(
            "settlement_status IN ('nominated', 'pending', 'partial', 'paid', 'failed')",
            name="ck_arena_daily_awards_settlement_status",
        ),
        CheckConstraint("paid_amount >= 0 AND paid_amount <= reward_amount", name="ck_arena_daily_awards_paid_amount_range"),
        UniqueConstraint("fund_ledger_key", name="uq_arena_daily_awards_fund_key"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    award_date: Mapped[date] = mapped_column(Date, nullable=False)
    award_key: Mapped[str] = mapped_column(String(40), nullable=False)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    reward_amount: Mapped[int] = mapped_column(BigInteger, nullable=False)
    fund_ledger_key: Mapped[str] = mapped_column(String(120), nullable=False)
    settlement_status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="nominated"
    )
    paid_amount: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    payment_source: Mapped[str | None] = mapped_column(String(16), nullable=True)
    details: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    awarded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class ArenaWeeklyAward(Base):
    """Idempotent weekly leaderboard reward."""

    __tablename__ = "arena_weekly_awards"
    __table_args__ = (
        UniqueConstraint(
            "chat_id",
            "week_start",
            "award_key",
            "user_id",
            name="uq_arena_weekly_awards_recipient",
        ),
        UniqueConstraint(
            "chat_id", "week_start", "award_key", name="uq_arena_weekly_awards_category"
        ),
        CheckConstraint("reward_amount >= 0", name="ck_arena_weekly_awards_reward_nonneg"),
        CheckConstraint(
            "settlement_status IN ('nominated', 'pending', 'partial', 'paid', 'failed')",
            name="ck_arena_weekly_awards_settlement_status",
        ),
        CheckConstraint("paid_amount >= 0 AND paid_amount <= reward_amount", name="ck_arena_weekly_awards_paid_amount_range"),
        UniqueConstraint("fund_ledger_key", name="uq_arena_weekly_awards_fund_key"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    week_start: Mapped[date] = mapped_column(Date, nullable=False)
    award_key: Mapped[str] = mapped_column(String(40), nullable=False)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    reward_amount: Mapped[int] = mapped_column(BigInteger, nullable=False)
    fund_ledger_key: Mapped[str] = mapped_column(String(120), nullable=False)
    settlement_status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="nominated"
    )
    paid_amount: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    payment_source: Mapped[str | None] = mapped_column(String(16), nullable=True)
    details: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    awarded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class ArenaDigestPublication(Base):
    """Идемпотентный статус ежедневной публикации Arena-дайджеста."""

    __tablename__ = "arena_digest_publications"
    __table_args__ = (
        UniqueConstraint(
            "chat_id", "digest_date", name="uq_arena_digest_publications_chat_date"
        ),
        CheckConstraint(
            "replay_status IN ('pending', 'sent', 'skipped', 'failed')",
            name="ck_arena_digest_publications_replay_status",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    digest_date: Mapped[date] = mapped_column(Date, nullable=False)
    best_match_id: Mapped[int | None] = mapped_column(
        ForeignKey("arena_matches.id", ondelete="SET NULL"), nullable=True
    )
    text_message_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    replay_message_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    replay_status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="pending")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class ArenaAdminAudit(Base):
    """Immutable audit record for manual Arena administration."""

    __tablename__ = "arena_admin_audit"
    __table_args__ = (
        UniqueConstraint("admin_id", "idempotency_key", name="uq_arena_admin_audit_idempotency"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    admin_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    operation: Mapped[str] = mapped_column(String(64), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(120), nullable=False)
    match_id: Mapped[int | None] = mapped_column(
        ForeignKey("arena_matches.id", ondelete="SET NULL"), nullable=True
    )
    amount: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    result: Mapped[str | None] = mapped_column(String(32), nullable=True)
    details: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
