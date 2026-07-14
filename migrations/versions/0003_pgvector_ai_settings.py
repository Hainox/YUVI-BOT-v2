"""pgvector activation + AI/NLP schema (message_embeddings, bot_settings, messages NLP columns)

Revision ID: 0003_pgvector_ai_settings
Revises: 0002_data_collection_schema
Create Date: 2026-07-14
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from pgvector.sqlalchemy import Vector

revision = "0003_pgvector_ai_settings"
down_revision = "0002_data_collection_schema"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # --- pgvector extension MUST be activated before any Vector(...) column is created (Pitfall 3) ---
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    # --- messages: NLP-колонки (NLP-01/NLP-02) ---
    op.add_column("messages", sa.Column("nlp_processed_at", sa.DateTime(), nullable=True))
    op.add_column("messages", sa.Column("sentiment_score", sa.Float(), nullable=True))
    op.add_column("messages", sa.Column("sentiment_label", sa.String(length=16), nullable=True))
    op.add_column("messages", sa.Column("toxicity_score", sa.Float(), nullable=True))

    # --- частичный индекс под 30-сек poll необработанных сообщений (Pitfall 6) ---
    op.execute(
        "CREATE INDEX ix_messages_nlp_unprocessed ON messages (id) WHERE nlp_processed_at IS NULL"
    )

    # --- GIN-индекс для лексической половины гибридного поиска /ask (FTS, russian) ---
    op.execute(
        "CREATE INDEX ix_messages_fts_ru ON messages "
        "USING gin (to_tsvector('russian', coalesce(text, '')))"
    )

    # --- message_embeddings (AI-04, RAG-поиск /ask) ---
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

    # --- HNSW-индекс для cosine ANN-поиска (RESEARCH A5 — работает и на пустой таблице) ---
    op.execute(
        "CREATE INDEX ix_message_embeddings_hnsw ON message_embeddings "
        "USING hnsw (embedding vector_cosine_ops)"
    )

    # --- bot_settings (AI-08, KV: модель/промпт) ---
    op.create_table(
        "bot_settings",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column("updated_by_tg_id", sa.BigInteger(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.UniqueConstraint("chat_id", "key", name="uq_bot_setting"),
    )


def downgrade() -> None:
    op.drop_table("bot_settings")

    op.execute("DROP INDEX IF EXISTS ix_message_embeddings_hnsw")
    op.drop_table("message_embeddings")

    op.execute("DROP INDEX IF EXISTS ix_messages_fts_ru")
    op.execute("DROP INDEX IF EXISTS ix_messages_nlp_unprocessed")

    op.drop_column("messages", "toxicity_score")
    op.drop_column("messages", "sentiment_label")
    op.drop_column("messages", "sentiment_score")
    op.drop_column("messages", "nlp_processed_at")

    op.execute("DROP EXTENSION IF EXISTS vector")
