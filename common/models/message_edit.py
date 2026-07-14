from __future__ import annotations

from sqlalchemy import BigInteger
from sqlalchemy import DateTime
from sqlalchemy import ForeignKey
from sqlalchemy import Text
from sqlalchemy import func
from sqlalchemy.orm import Mapped
from sqlalchemy.orm import mapped_column

from common.db.base import Base


class MessageEdit(Base):
    """Append-only журнал правок сообщения (D-03) — оригинал messages.text никогда не перезаписывается."""

    __tablename__ = "message_edits"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    message_id: Mapped[int] = mapped_column(ForeignKey("messages.id"), nullable=False)
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    telegram_message_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    new_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    edited_at: Mapped[DateTime] = mapped_column(DateTime, server_default=func.now())
