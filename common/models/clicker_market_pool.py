from __future__ import annotations

from sqlalchemy import BigInteger
from sqlalchemy import DateTime
from sqlalchemy import Float
from sqlalchemy import func
from sqlalchemy.orm import Mapped
from sqlalchemy.orm import mapped_column

from common.db.base import Base


class ClickerMarketPool(Base):
    """AMM-пул CP<->ювики на чат — одна строка на chat_id (форма chat_bank, D-03).

    r_cp/r_h — резервы constant-product пула (r_cp*r_h=k, mean-reversion через
    math.sqrt/exp в сервисе). Это ценообразующий пул, НЕ денежный баланс —
    реальные ювики остаются в user_balance/chat_bank через economy_service.
    Намеренно БЕЗ CHECK на резервы.
    """

    __tablename__ = "clicker_market_pool"

    chat_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    r_cp: Mapped[float] = mapped_column(Float, nullable=False)
    r_h: Mapped[float] = mapped_column(Float, nullable=False)
    updated_at: Mapped[DateTime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
