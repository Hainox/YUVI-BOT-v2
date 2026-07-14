"""mini app games schema (casino/farm/gacha/duel foundation)

Revision ID: 0005_mini_app_games
Revises: 0004_economy_betting_markets
Create Date: 2026-07-15
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0005_mini_app_games"
down_revision = "0004_economy_betting_markets"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # --- casino_games (04.1 foundation) ---
    op.create_table(
        "casino_games",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("game", sa.String(length=16), nullable=False),
        sa.Column("bet", sa.BigInteger(), nullable=False),
        sa.Column("payout", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("outcome", postgresql.JSONB(), nullable=True),
        sa.Column("state", postgresql.JSONB(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="settled"),
        sa.Column("idem_key", sa.String(length=80), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        # No CHECK on bet/payout: casino house-edge P&L is not blocked at model level
        # (same philosophy as chat_bank in 0004).
    )
    op.create_index("ix_casino_games_chat_user", "casino_games", ["chat_id", "user_id"])
    op.execute(
        "CREATE UNIQUE INDEX ux_casino_games_user_idem ON casino_games (user_id, idem_key) "
        "WHERE idem_key IS NOT NULL"
    )

    # --- clicker_farms (D-03) ---
    op.create_table(
        "clicker_farms",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("cp", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("tap_level", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("auto_level", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("pity_ssr", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("pity_ur", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "last_accrued_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.UniqueConstraint("chat_id", "user_id", name="uq_clicker_farms_chat_user"),
    )

    # --- clicker_market_pool (D-03) — one row per chat, chat_id is the PK ---
    op.create_table(
        "clicker_market_pool",
        sa.Column("chat_id", sa.BigInteger(), primary_key=True),
        sa.Column("r_cp", sa.Float(), nullable=False),
        sa.Column("r_h", sa.Float(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        # No CHECK: AMM reserves are pricing state, not money (economy_service holds money).
    )

    # --- clicker_market_price (D-03) — price snapshot history for the chart ---
    op.create_table(
        "clicker_market_price",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("price", sa.Float(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
    )
    op.create_index(
        "ix_clicker_market_price_chat_created",
        "clicker_market_price",
        ["chat_id", "created_at"],
    )

    # --- duels (D-08) — opponent_id NULL = duel vs. chat bank (/duelbot) ---
    op.create_table(
        "duels",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "challenger_id", sa.BigInteger(), sa.ForeignKey("users.id"), nullable=False
        ),
        sa.Column("opponent_id", sa.BigInteger(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("stake", sa.BigInteger(), nullable=False),
        # pending -> accepted -> resolved/declined/cancelled (mirrors markets.status)
        sa.Column("status", sa.String(length=16), nullable=False, server_default="pending"),
        sa.Column("winner_id", sa.BigInteger(), nullable=True),
        sa.Column("loser_id", sa.BigInteger(), nullable=True),
        sa.Column("fee", sa.BigInteger(), nullable=True),
        sa.Column("mute_seconds", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.Column("resolved_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_duels_chat_status", "duels", ["chat_id", "status"])

    # --- gacha_collection (D-03/D-07) — duplicate pull -> +1 star, up to 5 ---
    op.create_table(
        "gacha_collection",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("char_id", sa.String(length=64), nullable=False),
        sa.Column("stars", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("copies", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.UniqueConstraint(
            "user_id", "chat_id", "char_id", name="uq_gacha_collection_user_chat_char"
        ),
    )
    op.create_index("ix_gacha_collection_chat_user", "gacha_collection", ["chat_id", "user_id"])


def downgrade() -> None:
    op.drop_index("ix_gacha_collection_chat_user", table_name="gacha_collection")
    op.drop_table("gacha_collection")

    op.drop_index("ix_duels_chat_status", table_name="duels")
    op.drop_table("duels")

    op.drop_index("ix_clicker_market_price_chat_created", table_name="clicker_market_price")
    op.drop_table("clicker_market_price")

    op.drop_table("clicker_market_pool")

    op.drop_table("clicker_farms")

    op.execute("DROP INDEX IF EXISTS ux_casino_games_user_idem")
    op.drop_index("ix_casino_games_chat_user", table_name="casino_games")
    op.drop_table("casino_games")
