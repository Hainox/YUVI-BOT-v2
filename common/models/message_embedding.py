from __future__ import annotations

from sqlalchemy import BigInteger
from sqlalchemy import ForeignKey
from sqlalchemy.orm import Mapped
from sqlalchemy.orm import mapped_column

from pgvector.sqlalchemy import Vector

from common.db.base import Base


class MessageEmbedding(Base):
    """BGE-M3 1024-мерное представление сообщения для гибридного поиска /q."""

    __tablename__ = "message_embeddings_bge"

    message_id: Mapped[int] = mapped_column(
        ForeignKey("messages.id", ondelete="CASCADE"), primary_key=True
    )
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    embedding: Mapped[list[float]] = mapped_column(Vector(1024), nullable=False)
