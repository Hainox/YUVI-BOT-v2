"""initial schema

Revision ID: 0001_initial_schema
Revises:
Create Date: 2026-07-13
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0001_initial_schema"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("username", sa.String(length=64), nullable=True),
        sa.Column("first_name", sa.String(length=128), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
    )

    op.create_table(
        "messages",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("telegram_message_id", sa.BigInteger(), nullable=False),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("text", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
    )
    op.create_index("ix_messages_chat_id", "messages", ["chat_id"])
    op.create_index("ix_messages_telegram_message_id", "messages", ["telegram_message_id"])


def downgrade() -> None:
    op.drop_index("ix_messages_telegram_message_id", table_name="messages")
    op.drop_index("ix_messages_chat_id", table_name="messages")
    op.drop_table("messages")
    op.drop_table("users")

