"""Remove the retired Arena PvP feature."""

from __future__ import annotations

from alembic import op

revision = "0026_remove_arena_feature"
down_revision = "0025_remove_twin_feature"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("DROP TABLE IF EXISTS arena_digest_publications CASCADE")
    op.execute("DROP TABLE IF EXISTS arena_admin_audit CASCADE")
    op.execute("DROP TABLE IF EXISTS arena_daily_awards CASCADE")
    op.execute("DROP TABLE IF EXISTS arena_weekly_awards CASCADE")
    op.execute("DROP TABLE IF EXISTS arena_leaderboard_snapshots CASCADE")
    op.execute("DROP TABLE IF EXISTS arena_match_events CASCADE")
    op.execute("DROP TABLE IF EXISTS arena_matches CASCADE")
    op.execute("DROP TABLE IF EXISTS arena_fund_ledger CASCADE")
    op.execute("DROP TABLE IF EXISTS arena_funds CASCADE")
    op.execute("DROP TABLE IF EXISTS arena_fighter_progress CASCADE")
    op.execute("DROP TABLE IF EXISTS arena_profiles CASCADE")


def downgrade() -> None:
    # Historical Arena data is intentionally not recreated on downgrade: the
    # feature was removed and no safe schema/data contract remains for it.
    pass
