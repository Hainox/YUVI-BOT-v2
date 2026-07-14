"""data collection schema

Revision ID: 0002_data_collection_schema
Revises: 0001_initial_schema
Create Date: 2026-07-14
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0002_data_collection_schema"
down_revision = "0001_initial_schema"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # --- extend messages ---
    op.add_column(
        "messages",
        sa.Column("content_type", sa.String(length=32), nullable=False, server_default="text"),
    )
    op.add_column("messages", sa.Column("caption", sa.Text(), nullable=True))
    op.add_column(
        "messages",
        sa.Column("reply_to_telegram_message_id", sa.BigInteger(), nullable=True),
    )
    op.add_column("messages", sa.Column("message_thread_id", sa.BigInteger(), nullable=True))
    op.add_column("messages", sa.Column("media_file_id", sa.String(length=256), nullable=True))
    op.add_column(
        "messages",
        sa.Column("media_file_unique_id", sa.String(length=128), nullable=True),
    )
    op.add_column("messages", sa.Column("media_mime_type", sa.String(length=128), nullable=True))
    op.add_column("messages", sa.Column("media_file_size", sa.BigInteger(), nullable=True))
    op.add_column(
        "messages",
        sa.Column("is_forwarded", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "messages",
        sa.Column("source", sa.String(length=16), nullable=False, server_default="live"),
    )
    op.add_column("messages", sa.Column("deleted_at", sa.DateTime(), nullable=True))  # D-04

    op.create_unique_constraint(
        "uq_messages_chat_tg_id", "messages", ["chat_id", "telegram_message_id"]
    )

    # --- message_edits (D-03, append-only) ---
    op.create_table(
        "message_edits",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("message_id", sa.BigInteger(), sa.ForeignKey("messages.id"), nullable=False),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("telegram_message_id", sa.BigInteger(), nullable=False),
        sa.Column("new_text", sa.Text(), nullable=True),
        sa.Column("edited_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
    )
    op.create_index("ix_message_edits_message_id", "message_edits", ["message_id"])

    # --- reactions (current-state snapshot, internal FKs) ---
    op.create_table(
        "reactions",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("message_id", sa.BigInteger(), sa.ForeignKey("messages.id"), nullable=False),
        sa.Column("actor_user_id", sa.BigInteger(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("emoji", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.UniqueConstraint(
            "message_id", "actor_user_id", "emoji", name="uq_reaction_msg_actor_emoji"
        ),
    )
    op.create_index("ix_reactions_message_id", "reactions", ["message_id"])

    # --- word_frequency (per-user granular) ---
    op.create_table(
        "word_frequency",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("word", sa.String(length=128), nullable=False),
        sa.Column("count", sa.BigInteger(), nullable=False, server_default="0"),
        sa.UniqueConstraint("chat_id", "user_id", "word", name="uq_wordfreq"),
    )

    # --- emoji_frequency (same shape as word_frequency) ---
    op.create_table(
        "emoji_frequency",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("emoji", sa.String(length=64), nullable=False),
        sa.Column("count", sa.BigInteger(), nullable=False, server_default="0"),
        sa.UniqueConstraint("chat_id", "user_id", "emoji", name="uq_emojifreq"),
    )

    # --- daily_stats (per-user granular row) ---
    op.create_table(
        "daily_stats",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("stat_date", sa.Date(), nullable=False),
        sa.Column("message_count", sa.BigInteger(), nullable=False, server_default="0"),
        sa.UniqueConstraint("chat_id", "user_id", "stat_date", name="uq_dailystat"),
    )
    op.create_index("ix_daily_stats_chat_date", "daily_stats", ["chat_id", "stat_date"])


def downgrade() -> None:
    op.drop_index("ix_daily_stats_chat_date", table_name="daily_stats")
    op.drop_table("daily_stats")

    op.drop_table("emoji_frequency")

    op.drop_table("word_frequency")

    op.drop_index("ix_reactions_message_id", table_name="reactions")
    op.drop_table("reactions")

    op.drop_index("ix_message_edits_message_id", table_name="message_edits")
    op.drop_table("message_edits")

    op.drop_constraint("uq_messages_chat_tg_id", "messages", type_="unique")
    op.drop_column("messages", "deleted_at")
    op.drop_column("messages", "source")
    op.drop_column("messages", "is_forwarded")
    op.drop_column("messages", "media_file_size")
    op.drop_column("messages", "media_mime_type")
    op.drop_column("messages", "media_file_unique_id")
    op.drop_column("messages", "media_file_id")
    op.drop_column("messages", "message_thread_id")
    op.drop_column("messages", "reply_to_telegram_message_id")
    op.drop_column("messages", "caption")
    op.drop_column("messages", "content_type")
