"""slot_jackpots: progressive per-chat jackpot pool for slots (CASINO-06)

Revision ID: 0015_slot_jackpot
Revises: 0014_gacha_farm_level
Create Date: 2026-07-27
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0015_slot_jackpot"
down_revision = "0014_gacha_farm_level"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "slot_jackpots",
        sa.Column("chat_id", sa.BigInteger(), primary_key=True),
        sa.Column("pool", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("slot_jackpots")
