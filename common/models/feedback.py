from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger
from sqlalchemy import Boolean
from sqlalchemy import DateTime
from sqlalchemy import ForeignKey
from sqlalchemy import String
from sqlalchemy import Text
from sqlalchemy import func
from sqlalchemy.orm import Mapped
from sqlalchemy.orm import mapped_column

from common.db.base import Base


class Feedback(Base):
    """Заявка фидбека участника чата (CASINO-03, D-04/D-05).

    Плоские String-колонны для category — та же конвенция, что
    `CasinoGame.game`/`status`, `EconomyTx.kind`: НИКАКОГО native Postgres
    ENUM (расширение набора категорий — просто код-константа, не ALTER TYPE).

    reward/rewarded_at (фаза 6, D-14) — аудит и идемпотентность-якорь награды
    close(): rewarded_at IS NOT NULL означает, что награда уже выдана, повторный
    close() — no-op. Обе колонки nullable, заполняются только планом 06-05.
    """

    __tablename__ = "feedback"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    category: Mapped[str] = mapped_column(String(16), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    resolved: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    reward: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    rewarded_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
