from __future__ import annotations

from sqlalchemy import BigInteger
from sqlalchemy import DateTime
from sqlalchemy import String
from sqlalchemy import UniqueConstraint
from sqlalchemy import func
from sqlalchemy.orm import Mapped
from sqlalchemy.orm import mapped_column

from common.db.base import Base


class TwinOptIn(Base):
    """Согласие участника на AI-двойник (TWIN-02) — единый источник consent-гейта."""

    __tablename__ = "twin_opt_ins"
    __table_args__ = (UniqueConstraint("chat_id", "user_id", name="uq_twin_opt_in"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)  # 'active' | 'paused'
    updated_at: Mapped[DateTime] = mapped_column(DateTime, server_default=func.now())
