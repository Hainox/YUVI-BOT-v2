"""Arena award settlement state and partial payout tracking.

Revision ID: 0023_arena_award_settlement
Revises: 0022_arena_digest_publication
Create Date: 2026-08-08
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0023_arena_award_settlement"
down_revision = "0022_arena_digest_publication"
branch_labels = None
depends_on = None

_STATUS_SQL = "settlement_status IN ('nominated', 'pending', 'partial', 'paid', 'failed')"


def upgrade() -> None:
    for table in ("arena_daily_awards", "arena_weekly_awards"):
        op.add_column(
            table,
            sa.Column(
                "settlement_status",
                sa.String(length=16),
                nullable=False,
                server_default="nominated",
            ),
        )
        op.add_column(
            table,
            sa.Column("paid_amount", sa.BigInteger(), nullable=False, server_default="0"),
        )
        op.add_column(table, sa.Column("payment_source", sa.String(length=16), nullable=True))

    # Older nomination-only deployments could have inserted a second winner
    # for one category when the 21:50 job was retried after new matches. Keep
    # the first immutable nomination before adding the category-level guard.
    op.execute(
        sa.text(
            """
            DELETE FROM arena_daily_awards older
            USING arena_daily_awards newer
            WHERE older.chat_id = newer.chat_id
              AND older.award_date = newer.award_date
              AND older.award_key = newer.award_key
              AND older.id > newer.id
            """
        )
    )
    op.execute(
        sa.text(
            """
            DELETE FROM arena_weekly_awards older
            USING arena_weekly_awards newer
            WHERE older.chat_id = newer.chat_id
              AND older.week_start = newer.week_start
              AND older.award_key = newer.award_key
              AND older.id > newer.id
            """
        )
    )
    op.create_unique_constraint(
        "uq_arena_daily_awards_category",
        "arena_daily_awards",
        ["chat_id", "award_date", "award_key"],
    )
    op.create_unique_constraint(
        "uq_arena_weekly_awards_category",
        "arena_weekly_awards",
        ["chat_id", "week_start", "award_key"],
    )

    op.create_check_constraint(
        "ck_arena_daily_awards_settlement_status",
        "arena_daily_awards",
        _STATUS_SQL,
    )
    op.create_check_constraint(
        "ck_arena_daily_awards_paid_amount_range",
        "arena_daily_awards",
        "paid_amount >= 0 AND paid_amount <= reward_amount",
    )
    op.create_check_constraint(
        "ck_arena_weekly_awards_settlement_status",
        "arena_weekly_awards",
        _STATUS_SQL,
    )
    op.create_check_constraint(
        "ck_arena_weekly_awards_paid_amount_range",
        "arena_weekly_awards",
        "paid_amount >= 0 AND paid_amount <= reward_amount",
    )
    op.create_index(
        "ix_arena_daily_awards_settlement",
        "arena_daily_awards",
        ["chat_id", "settlement_status", "award_date"],
    )
    op.create_index(
        "ix_arena_weekly_awards_settlement",
        "arena_weekly_awards",
        ["chat_id", "settlement_status", "week_start"],
    )


def downgrade() -> None:
    op.drop_index("ix_arena_weekly_awards_settlement", table_name="arena_weekly_awards")
    op.drop_index("ix_arena_daily_awards_settlement", table_name="arena_daily_awards")
    op.drop_constraint("uq_arena_weekly_awards_category", "arena_weekly_awards", type_="unique")
    op.drop_constraint("uq_arena_daily_awards_category", "arena_daily_awards", type_="unique")
    op.drop_constraint(
        "ck_arena_weekly_awards_paid_amount_range",
        "arena_weekly_awards",
        type_="check",
    )
    op.drop_constraint(
        "ck_arena_weekly_awards_settlement_status",
        "arena_weekly_awards",
        type_="check",
    )
    op.drop_constraint(
        "ck_arena_daily_awards_paid_amount_range",
        "arena_daily_awards",
        type_="check",
    )
    op.drop_constraint(
        "ck_arena_daily_awards_settlement_status",
        "arena_daily_awards",
        type_="check",
    )
    for table in ("arena_weekly_awards", "arena_daily_awards"):
        op.drop_column(table, "payment_source")
        op.drop_column(table, "paid_amount")
        op.drop_column(table, "settlement_status")
