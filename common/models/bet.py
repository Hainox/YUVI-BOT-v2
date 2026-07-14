from __future__ import annotations

from sqlalchemy import BigInteger
from sqlalchemy import Boolean
from sqlalchemy import DateTime
from sqlalchemy import ForeignKey
from sqlalchemy import false
from sqlalchemy import func
from sqlalchemy.orm import Mapped
from sqlalchemy.orm import mapped_column

from common.db.base import Base


class Bet(Base):
    """Ставка участника на вариант рынка (BET-01)."""

    __tablename__ = "bets"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    market_id: Mapped[int] = mapped_column(ForeignKey("markets.id"), nullable=False)
    option_id: Mapped[int] = mapped_column(ForeignKey("market_options.id"), nullable=False)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    amount: Mapped[int] = mapped_column(BigInteger, nullable=False)
    payout: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    refunded: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=false())
    created_at: Mapped[DateTime] = mapped_column(DateTime, server_default=func.now())
