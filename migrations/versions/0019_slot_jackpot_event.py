"""slot_jackpots.event_spins_remaining: owner-triggered "guaranteed within N
spins" jackpot event (CASINO-06 follow-up, запрошено 2026-08-07 — пул вырос
до 800к+, владелец хочет ивент "гарант в следующих 100 крутках")

Revision ID: 0019_slot_jackpot_event
Revises: 0018_exchange_stuck_alert
Create Date: 2026-08-07
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0019_slot_jackpot_event"
down_revision = "0018_exchange_stuck_alert"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "slot_jackpots",
        sa.Column("event_spins_remaining", sa.BigInteger(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("slot_jackpots", "event_spins_remaining")
