from __future__ import annotations

from sqlalchemy import BigInteger
from sqlalchemy import DateTime
from sqlalchemy import ForeignKey
from sqlalchemy import Integer
from sqlalchemy import UniqueConstraint
from sqlalchemy import func
from sqlalchemy.orm import Mapped
from sqlalchemy.orm import mapped_column

from common.db.base import Base


class ClickerFarm(Base):
    """Ферма-кликер участника чата (D-03) — одна строка на (chat_id, user_id).

    pity_ssr/pity_ur живут прямо на строке фермы (Pattern 7) — гача-пачки
    тянутся из той же таблицы, что и тапы/автокликер.
    """

    __tablename__ = "clicker_farms"
    __table_args__ = (
        UniqueConstraint("chat_id", "user_id", name="uq_clicker_farms_chat_user"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    cp: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    tap_level: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    auto_level: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    pity_ssr: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    pity_ur: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_accrued_at: Mapped[DateTime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    created_at: Mapped[DateTime] = mapped_column(DateTime, server_default=func.now())
