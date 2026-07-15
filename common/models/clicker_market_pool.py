from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import BigInteger
from sqlalchemy import DateTime
from sqlalchemy import Numeric
from sqlalchemy import func
from sqlalchemy.orm import Mapped
from sqlalchemy.orm import mapped_column

from common.db.base import Base


class ClickerMarketPool(Base):
    """AMM-пул CP<->ювики на чат — одна строка на chat_id (форма chat_bank, D-03).

    r_cp/r_h — резервы constant-product пула (r_cp*r_h=k, mean-reversion через
    math.sqrt/exp в сервисе). Это ценообразующий пул, НЕ денежный баланс —
    реальные ювики остаются в user_balance/chat_bank через economy_service.
    Хранятся как Numeric (не Float): резервы напрямую определяют курс обмена
    на реальные ювики, а плавающая точка накапливает погрешность округления
    при повторных умножениях/делениях constant-product (CR-03).
    Намеренно БЕЗ CHECK на резервы.
    """

    __tablename__ = "clicker_market_pool"

    chat_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    r_cp: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    r_h: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )
