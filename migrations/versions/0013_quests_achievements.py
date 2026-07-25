"""quest_completions + achievement_unlocks: ежедневные квесты и ачивки,
разблокирующие бонусные уровни фермы (QUEST-01/02)

Revision ID: 0013_quests_achievements
Revises: 0012_changelog_entries
Create Date: 2026-07-25
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0013_quests_achievements"
down_revision = "0012_changelog_entries"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "quest_completions",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("quest_key", sa.String(length=32), nullable=False),
        sa.Column("day_msk", sa.Date(), nullable=False),
        sa.Column("reward_amount", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
    )
    op.create_index(
        "uq_quest_completion",
        "quest_completions",
        ["chat_id", "user_id", "quest_key", "day_msk"],
        unique=True,
    )

    op.create_table(
        "achievement_unlocks",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("achievement_key", sa.String(length=32), nullable=False),
        sa.Column("bonus_levels", sa.Integer(), nullable=False),
        sa.Column("reward_amount", sa.BigInteger(), nullable=False),
        sa.Column("unlocked_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
    )
    op.create_index(
        "uq_achievement_unlock",
        "achievement_unlocks",
        ["chat_id", "user_id", "achievement_key"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("uq_achievement_unlock", table_name="achievement_unlocks")
    op.drop_table("achievement_unlocks")
    op.drop_index("uq_quest_completion", table_name="quest_completions")
    op.drop_table("quest_completions")
