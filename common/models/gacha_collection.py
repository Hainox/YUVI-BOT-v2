from __future__ import annotations

from sqlalchemy import BigInteger
from sqlalchemy import DateTime
from sqlalchemy import ForeignKey
from sqlalchemy import Integer
from sqlalchemy import String
from sqlalchemy import UniqueConstraint
from sqlalchemy import func
from sqlalchemy.orm import Mapped
from sqlalchemy.orm import mapped_column

from common.db.base import Base


class GachaCollection(Base):
    """Коллекция гача-персонажей участника (D-03/D-07) — дубль -> +1 звезда, до 5.

    UniqueConstraint по (user_id, chat_id, char_id) — гача-сервис делает
    select-перед-insert по этой же тройке (Pattern 7).
    """

    __tablename__ = "gacha_collection"
    __table_args__ = (
        UniqueConstraint(
            "user_id", "chat_id", "char_id", name="uq_gacha_collection_user_chat_char"
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    char_id: Mapped[str] = mapped_column(String(64), nullable=False)
    stars: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    copies: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[DateTime] = mapped_column(DateTime, server_default=func.now())
