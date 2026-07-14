from __future__ import annotations

from sqlalchemy import BigInteger
from sqlalchemy import ForeignKey
from sqlalchemy import String
from sqlalchemy import UniqueConstraint
from sqlalchemy.orm import Mapped
from sqlalchemy.orm import mapped_column

from common.db.base import Base


class EmojiFrequency(Base):
    """Per-user частотный словарь эмодзи (та же форма, что WordFrequency)."""

    __tablename__ = "emoji_frequency"
    __table_args__ = (
        UniqueConstraint("chat_id", "user_id", "emoji", name="uq_emojifreq"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    emoji: Mapped[str] = mapped_column(String(64), nullable=False)
    count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
