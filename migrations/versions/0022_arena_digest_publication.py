"""Arena daily digest and replay publication state.

Revision ID: 0022_arena_digest_publication
Revises: 0021_autobattle
Create Date: 2026-08-08
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0022_arena_digest_publication"
down_revision = "0021_autobattle"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "arena_digest_publications",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("digest_date", sa.Date(), nullable=False),
        sa.Column(
            "best_match_id",
            sa.BigInteger(),
            sa.ForeignKey("arena_matches.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("text_message_id", sa.Integer(), nullable=True),
        sa.Column("replay_message_id", sa.Integer(), nullable=True),
        sa.Column("replay_status", sa.String(length=16), nullable=False, server_default="pending"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint(
            "chat_id", "digest_date", name="uq_arena_digest_publications_chat_date"
        ),
        sa.CheckConstraint(
            "replay_status IN ('pending', 'sent', 'skipped', 'failed')",
            name="ck_arena_digest_publications_replay_status",
        ),
    )
    op.create_index(
        "ix_arena_digest_publications_chat_date",
        "arena_digest_publications",
        ["chat_id", "digest_date"],
    )


def downgrade() -> None:
    op.drop_index("ix_arena_digest_publications_chat_date", table_name="arena_digest_publications")
    op.drop_table("arena_digest_publications")
