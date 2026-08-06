"""exchange_listings.stuck_alert_sent_at: idempotency-гард для фонового
alert_stuck_listings (найдено ревью 2026-08-05)

claimed-листинг, чей продавец пропал после того, как покупатель уже
заплатил вне бота, раньше не имел ни фонового job'а, ни алерта, ни aging-
видимости для админов (в отличие от любой другой stateful-сущности бота).
Эта колонка — NULL, пока алерт про конкретный зависший листинг ещё не
отправлен; ставится ровно один раз, когда bot/services/exchange_service.py
::alert_stuck_listings шлёт предупреждение в чат, чтобы повторные тики
job'а (interval 60м, bot/services/scheduler.py) не дублировали алерт.

Revision ID: 0018_exchange_stuck_alert
Revises: 0017_clicker_upgrade_idempotency
Create Date: 2026-08-05
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0018_exchange_stuck_alert"
down_revision = "0017_clicker_upgrade_idempotency"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "exchange_listings",
        sa.Column("stuck_alert_sent_at", sa.DateTime(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("exchange_listings", "stuck_alert_sent_at")
