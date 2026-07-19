"""active_title_ref_id: active_titles.ref_id (05-REVIEW.md WR-01)

tag_rental_service.rent_title's idempotent-retry path previously picked the
"most recent rental row" for the user, which could return a stale/unrelated
row if the user rented again with a different ref_id before the retry
arrived. Storing the ref_id that created the row lets the retry path look
up the EXACT row this ref_id produced, instead of guessing by recency.

Revision ID: 0009_active_title_ref_id
Revises: 0008_daily_rituals_tags_twin
Create Date: 2026-07-19
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0009_active_title_ref_id"
down_revision = "0008_daily_rituals_tags_twin"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "active_titles",
        sa.Column("ref_id", sa.String(length=80), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("active_titles", "ref_id")
