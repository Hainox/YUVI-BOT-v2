"""gacha_collection.farm_level: индивидуальная прокачка героини в ферме
(GACHA-05, idle-game-стиль апгрейда отдельно от созвездия/дублей)

Revision ID: 0014_gacha_farm_level
Revises: 0013_quests_achievements
Create Date: 2026-07-25
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0014_gacha_farm_level"
down_revision = "0013_quests_achievements"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "gacha_collection",
        sa.Column("farm_level", sa.Integer(), nullable=False, server_default="1"),
    )


def downgrade() -> None:
    op.drop_column("gacha_collection", "farm_level")
