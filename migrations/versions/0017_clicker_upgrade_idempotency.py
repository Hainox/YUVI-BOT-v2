"""clicker_upgrade_log: ref_id-идемпотентность апгрейдов тапа/автокликера/
героини фермы (bugfix аудита 2026-08-05 — сетевой ретрай раньше мог
списать CP и поднять уровень дважды за одно нажатие)

Revision ID: 0017_clicker_upgrade_idempotency
Revises: 0016_daily_twin_posts
Create Date: 2026-08-05
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0017_clicker_upgrade_idempotency"
down_revision = "0016_daily_twin_posts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "clicker_upgrade_log",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("kind", sa.String(length=24), nullable=False),
        sa.Column("ref_id", sa.String(length=80), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("chat_id", "ref_id", "kind", name="uq_clicker_upgrade_log_ref_kind"),
    )
    op.create_index(
        "ix_clicker_upgrade_log_chat_user", "clicker_upgrade_log", ["chat_id", "user_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_clicker_upgrade_log_chat_user", table_name="clicker_upgrade_log")
    op.drop_table("clicker_upgrade_log")
