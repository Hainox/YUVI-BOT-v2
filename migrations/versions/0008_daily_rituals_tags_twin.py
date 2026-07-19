"""daily_rituals_tags_twin: daily_picks, active_titles, twin_opt_ins,
daily_stats +4 columns (VICTIM-01/02, AWARDS-01/02, LOTTERY-01, TAG-01/02, TWIN-01/02)

Revision ID: 0008_daily_rituals_tags_twin
Revises: 0007_feedback
Create Date: 2026-07-19
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0008_daily_rituals_tags_twin"
down_revision = "0007_feedback"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # --- daily_picks (VICTIM-01/02, LOTTERY-01, D-09) ---
    op.create_table(
        "daily_picks",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),  # 'victim' | 'lottery'
        sa.Column("day_msk", sa.Date(), nullable=False),
        sa.Column("winner_user_id", sa.BigInteger(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()")),
    )
    op.create_unique_constraint(
        "uq_daily_pick", "daily_picks", ["chat_id", "kind", "day_msk"]
    )

    # --- active_titles (TAG-01/02, D-07/D-10) ---
    op.create_table(
        "active_titles",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("tg_user_id", sa.BigInteger(), nullable=False),
        sa.Column("title", sa.String(length=16), nullable=False),
        sa.Column("source", sa.String(length=16), nullable=False),  # 'victim' | 'rental'
        sa.Column("price_paid", sa.BigInteger(), nullable=True),
        sa.Column("granted_at", sa.DateTime(), server_default=sa.text("now()")),
        sa.Column("expires_at", sa.DateTime(), nullable=True),
        # 'active' | 'suspended' | 'expired' | 'cancelled'
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()")),
    )
    # Ровно один активный титул на участника (D-10) — второй активный INSERT
    # структурно невозможен, фундамент для приоритета жертва-над-арендой (05-03/05-07).
    op.execute(
        "CREATE UNIQUE INDEX uq_active_title_user_active ON active_titles "
        "(chat_id, user_id) WHERE status = 'active'"
    )
    # Скан просроченных титулов/аренд планировщиком.
    op.create_index(
        "ix_active_title_status_expires", "active_titles", ["status", "expires_at"]
    )
    op.create_index("ix_active_title_chat_status", "active_titles", ["chat_id", "status"])

    # --- twin_opt_ins (TWIN-02) ---
    op.create_table(
        "twin_opt_ins",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),  # 'active' | 'paused'
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("now()")),
    )
    op.create_unique_constraint(
        "uq_twin_opt_in", "twin_opt_ins", ["chat_id", "user_id"]
    )

    # --- daily_stats +4 columns (AWARDS-01 метрики) ---
    op.add_column(
        "daily_stats",
        sa.Column(
            "profanity_count", sa.BigInteger(), nullable=False, server_default=sa.text("0")
        ),
    )
    op.add_column(
        "daily_stats",
        sa.Column("photo_count", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
    )
    op.add_column(
        "daily_stats",
        sa.Column("forward_count", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
    )
    op.add_column(
        "daily_stats",
        sa.Column(
            "longest_msg_len", sa.BigInteger(), nullable=False, server_default=sa.text("0")
        ),
    )


def downgrade() -> None:
    op.drop_column("daily_stats", "longest_msg_len")
    op.drop_column("daily_stats", "forward_count")
    op.drop_column("daily_stats", "photo_count")
    op.drop_column("daily_stats", "profanity_count")

    op.drop_table("twin_opt_ins")

    op.drop_index("ix_active_title_chat_status", table_name="active_titles")
    op.drop_index("ix_active_title_status_expires", table_name="active_titles")
    op.execute("DROP INDEX IF EXISTS uq_active_title_user_active")
    op.drop_table("active_titles")

    op.drop_table("daily_picks")
