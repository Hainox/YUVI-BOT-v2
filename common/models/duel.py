from __future__ import annotations

from sqlalchemy import BigInteger
from sqlalchemy import DateTime
from sqlalchemy import ForeignKey
from sqlalchemy import Integer
from sqlalchemy import String
from sqlalchemy import func
from sqlalchemy.orm import Mapped
from sqlalchemy.orm import mapped_column

from common.db.base import Base


class Duel(Base):
    """PvP-дуэль на ставку (D-08) — opponent_id NULL = дуэль с банком чата (/duelbot).

    status следует форме markets.status: pending -> accepted -> resolved/declined/cancelled.
    Индекс (chat_id, status) создаётся в миграции 0005, не здесь.
    """

    __tablename__ = "duels"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    challenger_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    opponent_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    stake: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    winner_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    loser_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    fee: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    mute_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[DateTime] = mapped_column(DateTime, server_default=func.now())
    resolved_at: Mapped[DateTime | None] = mapped_column(DateTime, nullable=True)
