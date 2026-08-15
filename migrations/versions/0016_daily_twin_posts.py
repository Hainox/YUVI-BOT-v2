"""daily_twin_posts: журнал постов "дневного двойника" (TWIN-03)

Revision ID: 0016_daily_twin_posts
Revises: 0015_slot_jackpot
Create Date: 2026-07-27
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0016_daily_twin_posts"
down_revision = "0015_slot_jackpot"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "daily_twin_posts",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("telegram_message_id", sa.BigInteger(), nullable=False),
        sa.Column("target_user_id", sa.BigInteger(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("day_msk", sa.Date(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("chat_id", "telegram_message_id", name="uq_daily_twin_post"),
    )


def downgrade() -> None:
    op.drop_table("daily_twin_posts")
