from __future__ import annotations

from sqlalchemy import BigInteger
from sqlalchemy import ForeignKey
from sqlalchemy import String
from sqlalchemy import UniqueConstraint
from sqlalchemy.orm import Mapped
from sqlalchemy.orm import mapped_column

from common.db.base import Base


class WordFrequency(Base):
    """Per-user частотный словарь слов (без chat-wide NULL-строки)."""

    __tablename__ = "word_frequency"
    __table_args__ = (
        UniqueConstraint("chat_id", "user_id", "word", name="uq_wordfreq"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    word: Mapped[str] = mapped_column(String(128), nullable=False)
    count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
