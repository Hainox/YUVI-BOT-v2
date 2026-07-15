"""clicker farm: dedicated server-clock column for tap anti-cheat (fixes CR-02)

Revision ID: 0006_clicker_tap_anticheat
Revises: 0005_mini_app_games
Create Date: 2026-07-15
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0006_clicker_tap_anticheat"
down_revision = "0005_mini_app_games"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # last_accrued_at is shared with the offline-accrual read path (get_farm_state
    # resets it on every poll), so it cannot double as the tap anti-cheat clock —
    # a client-supplied elapsed_ms was the only input to the CPS clamp (CR-02).
    # last_tap_at is written ONLY by tap(), giving the clamp a server-observed
    # interval that a client cannot inflate.
    op.add_column(
        "clicker_farms",
        sa.Column("last_tap_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
    )


def downgrade() -> None:
    op.drop_column("clicker_farms", "last_tap_at")
