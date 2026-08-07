"""slot_jackpots.event_spins_remaining: owner-triggered "guaranteed within N
spins" jackpot event (CASINO-06 follow-up, запрошено 2026-08-07 — пул вырос
до 800к+, владелец хочет ивент "гарант в следующих 100 крутках")

Renumbered 0019 -> 0020 (найдено CI: "Multiple head revisions") — Arena
(0019_arena_phase_2) смёржилась в main из отдельной ветки/сессии тем же
"следующим" номером 0019, независимо от этой ветки, оба смотрели на один и
тот же down_revision=0018 и разветвили граф миграций. Перецепляем эту
миграцию ПОСЛЕ arena, а не наоборот — arena уже в main, эта ветка ещё нет.

Revision ID: 0020_slot_jackpot_event
Revises: 0019_arena_phase_2
Create Date: 2026-08-07
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0020_slot_jackpot_event"
down_revision = "0019_arena_phase_2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "slot_jackpots",
        sa.Column("event_spins_remaining", sa.BigInteger(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("slot_jackpots", "event_spins_remaining")
