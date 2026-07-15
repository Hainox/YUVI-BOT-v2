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


class ClickerMarketPrice(Base):
    """Снапшот курса AMM-пула фермы для графика цены (D-03).

    Индекс (chat_id, created_at) создаётся в миграции 0005, не здесь.
    Numeric (не Float), т.к. цена — снапшот того же ценообразующего пула,
    что и clicker_market_pool.r_cp/r_h (CR-03); плавающая точка ломает
    консистентность истории с текущим состоянием пула.
    """

    __tablename__ = "clicker_market_price"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    price: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
