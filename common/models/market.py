from __future__ import annotations

from sqlalchemy import BigInteger
from sqlalchemy import DateTime
from sqlalchemy import ForeignKey
from sqlalchemy import Integer
from sqlalchemy import String
from sqlalchemy import Text
from sqlalchemy import func
from sqlalchemy.orm import Mapped
from sqlalchemy.orm import mapped_column

from common.db.base import Base


class Market(Base):
    """Parimutuel-рынок ставок (BET-01/BET-02) — internal|polymarket|manifold."""

    __tablename__ = "markets"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    type: Mapped[str] = mapped_column(String(16), nullable=False)
    question: Mapped[str] = mapped_column(String(400), nullable=False)
    creator_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="open")
    closes_at: Mapped[DateTime] = mapped_column(DateTime, nullable=False)
    winning_option_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    external_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    external_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    created_at: Mapped[DateTime] = mapped_column(DateTime, server_default=func.now())


class MarketOption(Base):
    """Вариант ответа рынка ставок с собственным parimutuel-пулом (BET-01)."""

    __tablename__ = "market_options"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    market_id: Mapped[int] = mapped_column(
        ForeignKey("markets.id", ondelete="CASCADE"), nullable=False
    )
    label: Mapped[str] = mapped_column(String(200), nullable=False)
    pool: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
