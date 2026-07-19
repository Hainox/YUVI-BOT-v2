from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger
from sqlalchemy import DateTime
from sqlalchemy import ForeignKey
from sqlalchemy import String
from sqlalchemy import func
from sqlalchemy.orm import Mapped
from sqlalchemy.orm import mapped_column

from common.db.base import Base


class ActiveTitle(Base):
    """Активный Telegram custom_title участника — жертва дня + рынок аренды
    тегов (TAG-01/02, D-07/D-10).

    source ∈ {'victim', 'rental'} — как и status, НИКАКОГО native Postgres
    ENUM (форма casino_game.py). Частичный UNIQUE(chat_id, user_id) WHERE
    status='active' — ровно один активный титул на участника — создаётся в
    миграции 0008 (05-RESEARCH.md Pattern 1), не в модели (форма
    casino_game.py "частичный UNIQUE ... создаётся в миграции").
    """

    __tablename__ = "active_titles"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    tg_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    title: Mapped[str] = mapped_column(String(16), nullable=False)
    source: Mapped[str] = mapped_column(String(16), nullable=False)  # 'victim' | 'rental'
    price_paid: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    granted_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # 'active' | 'suspended' | 'expired' | 'cancelled'
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
