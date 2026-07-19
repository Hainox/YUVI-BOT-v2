from __future__ import annotations

from datetime import date
from datetime import datetime

from sqlalchemy import BigInteger
from sqlalchemy import Date
from sqlalchemy import DateTime
from sqlalchemy import ForeignKey
from sqlalchemy import String
from sqlalchemy import UniqueConstraint
from sqlalchemy import func
from sqlalchemy.orm import Mapped
from sqlalchemy.orm import mapped_column

from common.db.base import Base


class DailyPick(Base):
    """Ежедневный идемпотентный пик (жертва дня / лотерея) — VICTIM-01/02,
    LOTTERY-01, D-09.

    UNIQUE(chat_id, kind, day_msk) — день входит в ключ уникальности, поэтому
    новый день структурно всегда свежая строка: явный DELETE-сброс НЕ нужен
    (05-RESEARCH.md Pitfall 4). kind ∈ {'victim', 'lottery'}. expires_at
    используется только для kind='victim' (окно 24ч дебаффа/титула);
    lottery-строки его не используют — чисто анонс без экономического эффекта.
    """

    __tablename__ = "daily_picks"
    __table_args__ = (UniqueConstraint("chat_id", "kind", "day_msk", name="uq_daily_pick"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)  # 'victim' | 'lottery'
    day_msk: Mapped[date] = mapped_column(Date, nullable=False)
    winner_user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
