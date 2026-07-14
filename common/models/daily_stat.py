from __future__ import annotations

from sqlalchemy import BigInteger
from sqlalchemy import Date
from sqlalchemy import ForeignKey
from sqlalchemy import UniqueConstraint
from sqlalchemy.orm import Mapped
from sqlalchemy.orm import mapped_column

from common.db.base import Base


class DailyStat(Base):
    """Per-user гранулярная строка дневной статистики (user_id FK not null)."""

    __tablename__ = "daily_stats"
    __table_args__ = (
        UniqueConstraint("chat_id", "user_id", "stat_date", name="uq_dailystat"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    stat_date: Mapped[Date] = mapped_column(Date, nullable=False)
    message_count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
