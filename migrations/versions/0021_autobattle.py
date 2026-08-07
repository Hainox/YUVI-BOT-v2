"""autobattles: PvP-автобитва гача-коллекциями на ставку (GACHA-06)

Revision ID: 0021_autobattle
Revises: 0020_slot_jackpot_event
Create Date: 2026-08-07
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0021_autobattle"
down_revision = "0020_slot_jackpot_event"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "autobattles",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("challenger_id", sa.BigInteger(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("opponent_id", sa.BigInteger(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("stake", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("winner_id", sa.BigInteger(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("loser_id", sa.BigInteger(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("challenger_power", sa.BigInteger(), nullable=True),
        sa.Column("opponent_power", sa.BigInteger(), nullable=True),
        sa.Column("fee", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("resolved_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_autobattles_chat_status", "autobattles", ["chat_id", "status"])


def downgrade() -> None:
    op.drop_index("ix_autobattles_chat_status", table_name="autobattles")
    op.drop_table("autobattles")
