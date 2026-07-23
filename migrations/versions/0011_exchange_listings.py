"""exchange_listings: P2P-биржа ювиков (EXCHANGE-01)

Продавец эскроирует ювики, покупатель claim'ит листинг (сигнал координации,
не платёж), продавец подтверждает получение оплаты вне бота и освобождает
эскроу. item_type/gacha_char_id — схема на будущее (гача-карты), сам флоу
карточных листингов не реализован в этой фазе.

Revision ID: 0011_exchange_listings
Revises: 0010_feedback_reward
Create Date: 2026-07-23
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0011_exchange_listings"
down_revision = "0010_feedback_reward"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "exchange_listings",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("seller_user_id", sa.BigInteger(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("yuvik_amount", sa.BigInteger(), nullable=False),
        sa.Column("want_description", sa.String(length=300), nullable=False),
        # open -> claimed -> fulfilled, либо open/claimed -> cancelled (mirrors duels.status)
        sa.Column("status", sa.String(length=16), nullable=False, server_default="open"),
        # yuvik|gacha_card — схема на будущее, флоу карточных листингов не реализован
        sa.Column("item_type", sa.String(length=16), nullable=False, server_default="yuvik"),
        sa.Column("gacha_char_id", sa.String(length=64), nullable=True),
        sa.Column("claimed_by_user_id", sa.BigInteger(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("claimed_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.Column("resolved_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_exchange_listings_chat_status", "exchange_listings", ["chat_id", "status"])
    op.create_index("ix_exchange_listings_chat_seller", "exchange_listings", ["chat_id", "seller_user_id"])
    op.create_index(
        "ix_exchange_listings_chat_claimed_by", "exchange_listings", ["chat_id", "claimed_by_user_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_exchange_listings_chat_claimed_by", table_name="exchange_listings")
    op.drop_index("ix_exchange_listings_chat_seller", table_name="exchange_listings")
    op.drop_index("ix_exchange_listings_chat_status", table_name="exchange_listings")
    op.drop_table("exchange_listings")
