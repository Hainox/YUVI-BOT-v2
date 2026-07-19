"""feedback_reward: feedback.reward + feedback.rewarded_at (фаза 6, D-14)

Аудит и идемпотентность-якорь награды close() из плана 06-05: rewarded_at
IS NOT NULL означает, что награда уже выдана, повторный close() — no-op.
Обе колонки nullable — не ломают существующие строки feedback.

Revision ID: 0010_feedback_reward
Revises: 0009_active_title_ref_id
Create Date: 2026-07-20
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0010_feedback_reward"
down_revision = "0009_active_title_ref_id"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "feedback",
        sa.Column("reward", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "feedback",
        sa.Column("rewarded_at", sa.DateTime(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("feedback", "rewarded_at")
    op.drop_column("feedback", "reward")
