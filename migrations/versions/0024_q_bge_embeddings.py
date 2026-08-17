"""Replace legacy 768-dim embeddings with BGE-M3 1024-dim embeddings for /q.

Existing vectors cannot be reused because their dimensionality and model space
are different. The table is intentionally rebuilt empty; embed_worker fills it
again from messages after the migration.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from pgvector.sqlalchemy import Vector

revision = "0024_q_bge_embeddings"
down_revision = "0023_arena_award_settlement"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_message_embeddings_hnsw")
    op.execute("DROP TABLE IF EXISTS message_embeddings")
    op.create_table(
        "message_embeddings_bge",
        sa.Column(
            "message_id",
            sa.BigInteger(),
            sa.ForeignKey("messages.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("embedding", Vector(1024), nullable=False),
    )
    op.execute(
        "CREATE INDEX ix_message_embeddings_bge_hnsw ON message_embeddings_bge "
        "USING hnsw (embedding vector_cosine_ops)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_message_embeddings_bge_hnsw")
    op.drop_table("message_embeddings_bge")
    op.create_table(
        "message_embeddings",
        sa.Column(
            "message_id",
            sa.BigInteger(),
            sa.ForeignKey("messages.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("embedding", Vector(768), nullable=False),
    )
    op.execute(
        "CREATE INDEX ix_message_embeddings_hnsw ON message_embeddings "
        "USING hnsw (embedding vector_cosine_ops)"
    )
