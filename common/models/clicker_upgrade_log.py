from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger
from sqlalchemy import DateTime
from sqlalchemy import ForeignKey
from sqlalchemy import String
from sqlalchemy import UniqueConstraint
from sqlalchemy import func
from sqlalchemy.orm import Mapped
from sqlalchemy.orm import mapped_column

from common.db.base import Base


class ClickerUpgradeLog(Base):
    """Идемпотентный append-only журнал CP-only апгрейдов фермы (тап/
    автокликер/героиня) — bugfix аудита 2026-08-05 (upgrade_tap/upgrade_auto/
    upgrade_character раньше не имели ref_id-защиты от повторной отправки,
    единственные денежно-движущие пути `clicker_service.py` без неё).

    Та же SAVEPOINT + UNIQUE(chat_id, ref_id, kind) + IntegrityError-on-replay
    идиома, что `economy_tx`/`ux_economy_tx_ref_id_kind`
    (`economy_service.credit`/`debit`) — но СВОЯ таблица, не `economy_tx`:
    эти три апгрейда тратят исключительно внутренний CP фермы (`ClickerFarm.
    cp`), НИКОГДА не двигают ювики (см. докстринг `clicker_service.py`), а
    `economy_tx` зарезервирован под операции РЕАЛЬНОЙ валюты (ECON-03).
    `kind` разделяет три вида апгрейда ('tap'|'auto'|'character') в одном
    общем ref_id-неймспейсе на чат, как `kind` в `economy_tx`. Строки только
    INSERT, никогда не UPDATE/DELETE.
    """

    __tablename__ = "clicker_upgrade_log"
    __table_args__ = (
        UniqueConstraint("chat_id", "ref_id", "kind", name="uq_clicker_upgrade_log_ref_kind"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    kind: Mapped[str] = mapped_column(String(24), nullable=False)  # 'tap' | 'auto' | 'character'
    ref_id: Mapped[str] = mapped_column(String(80), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
