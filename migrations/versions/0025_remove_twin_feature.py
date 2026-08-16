"""Remove the retired AI twin tables."""

from __future__ import annotations

from alembic import op

revision = "0025_remove_twin_feature"
down_revision = "0024_q_bge_embeddings"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("DROP TABLE IF EXISTS daily_twin_posts")
    op.execute("DROP TABLE IF EXISTS twin_opt_ins")


def downgrade() -> None:
    # Historical twin data is intentionally not recreated on downgrade: the
    # feature was removed and no safe schema/data contract remains for it.
    pass
