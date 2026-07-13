from __future__ import annotations

from sqlalchemy import BigInteger
from sqlalchemy import DateTime
from sqlalchemy import ForeignKey
from sqlalchemy import Text
from sqlalchemy import func
from sqlalchemy.orm import Mapped
from sqlalchemy.orm import mapped_column

from common.db.base import Base


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    telegram_message_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    text: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[DateTime] = mapped_column(DateTime, server_default=func.now())

