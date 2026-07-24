"""changelog_entries: лента «Что нового» (WHATSNEW-01)

Обновления и планы разработки, публикуемые владельцем бота через
/post_update. Глобальная лента, без chat_id.

Revision ID: 0012_changelog_entries
Revises: 0011_exchange_listings
Create Date: 2026-07-24
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0012_changelog_entries"
down_revision = "0011_exchange_listings"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "changelog_entries",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("body", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
    )
    op.create_index("ix_changelog_entries_created_at", "changelog_entries", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_changelog_entries_created_at", table_name="changelog_entries")
    op.drop_table("changelog_entries")
