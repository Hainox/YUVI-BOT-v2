"""economy + betting markets schema (ECON-01..03, BET-01..03)

Revision ID: 0004_economy_betting_markets
Revises: 0003_pgvector_ai_settings
Create Date: 2026-07-14
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0004_economy_betting_markets"
down_revision = "0003_pgvector_ai_settings"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # --- user_balance (ECON-01) ---
    op.create_table(
        "user_balance",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("balance", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.UniqueConstraint("chat_id", "user_id", name="uq_user_balance_chat_user"),
        sa.CheckConstraint("balance >= 0", name="ck_user_balance_nonneg"),
    )

    # --- chat_bank (ECON-01) — chat_id is the PK directly, one row per chat ---
    op.create_table(
        "chat_bank",
        sa.Column("chat_id", sa.BigInteger(), primary_key=True),
        sa.Column("balance", sa.BigInteger(), nullable=False, server_default="0"),
        # No CHECK >= 0: casino (Phase 4) legitimately runs the bank negative (house edge P&L).
    )

    # --- economy_tx (ECON-03) — append-only ledger, never UPDATE/DELETE a row ---
    op.create_table(
        "economy_tx",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        # NULL user_id = chat-bank-side entry of a fee/mint/sink (mirrors reference bot).
        sa.Column("user_id", sa.BigInteger(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("amount", sa.BigInteger(), nullable=False),  # signed: + credit, - debit
        sa.Column("kind", sa.String(length=40), nullable=False),
        sa.Column("ref_id", sa.String(length=80), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
    )
    op.create_index("ix_economy_tx_chat_user", "economy_tx", ["chat_id", "user_id"])
    op.create_index("ix_economy_tx_chat_kind", "economy_tx", ["chat_id", "kind"])
    op.create_index("ix_economy_tx_created_at", "economy_tx", ["created_at"])
    op.execute(
        "CREATE UNIQUE INDEX ux_economy_tx_ref_id_kind ON economy_tx (chat_id, ref_id, kind) "
        "WHERE ref_id IS NOT NULL"
    )

    # --- markets (BET-01/02) ---
    op.create_table(
        "markets",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("type", sa.String(length=16), nullable=False),  # internal|polymarket|manifold
        sa.Column("question", sa.String(length=400), nullable=False),
        sa.Column("creator_id", sa.BigInteger(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="open"),
        # open -> closed -> resolved, or open/closed -> cancelled
        sa.Column("closes_at", sa.DateTime(), nullable=False),
        sa.Column("winning_option_id", sa.BigInteger(), nullable=True),
        sa.Column("external_url", sa.Text(), nullable=True),
        sa.Column("external_id", sa.String(length=120), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
    )
    op.create_index("ix_markets_chat_status", "markets", ["chat_id", "status"])
    # Dedup imported external markets per chat (external_markets.py's import_market).
    op.execute(
        "CREATE UNIQUE INDEX ux_markets_chat_type_external ON markets (chat_id, type, external_id) "
        "WHERE external_id IS NOT NULL"
    )

    # --- market_options (BET-01) — parimutuel pools ---
    op.create_table(
        "market_options",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "market_id", sa.BigInteger(), sa.ForeignKey("markets.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("label", sa.String(length=200), nullable=False),
        sa.Column("pool", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("position", sa.Integer(), nullable=False),  # 1-based display order
    )
    op.create_index("ix_market_options_market_id", "market_options", ["market_id"])

    # --- bets (BET-01) ---
    op.create_table(
        "bets",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("market_id", sa.BigInteger(), sa.ForeignKey("markets.id"), nullable=False),
        sa.Column("option_id", sa.BigInteger(), sa.ForeignKey("market_options.id"), nullable=False),
        sa.Column("user_id", sa.BigInteger(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("amount", sa.BigInteger(), nullable=False),
        sa.Column("payout", sa.BigInteger(), nullable=True),
        sa.Column("refunded", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
    )
    op.create_index("ix_bets_market_id", "bets", ["market_id"])
    op.create_index("ix_bets_user_id", "bets", ["user_id"])


def downgrade() -> None:
    op.drop_table("bets")
    op.drop_index("ix_market_options_market_id", table_name="market_options")
    op.drop_table("market_options")
    op.execute("DROP INDEX IF EXISTS ux_markets_chat_type_external")
    op.drop_index("ix_markets_chat_status", table_name="markets")
    op.drop_table("markets")
    op.execute("DROP INDEX IF EXISTS ux_economy_tx_ref_id_kind")
    op.drop_index("ix_economy_tx_created_at", table_name="economy_tx")
    op.drop_index("ix_economy_tx_chat_kind", table_name="economy_tx")
    op.drop_index("ix_economy_tx_chat_user", table_name="economy_tx")
    op.drop_table("economy_tx")
    op.drop_table("chat_bank")
    op.drop_table("user_balance")
