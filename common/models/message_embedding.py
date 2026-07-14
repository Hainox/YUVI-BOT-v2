from __future__ import annotations

from sqlalchemy import BigInteger
from sqlalchemy import ForeignKey
from sqlalchemy.orm import Mapped
from sqlalchemy.orm import mapped_column

from pgvector.sqlalchemy import Vector

from common.db.base import Base


class MessageEmbedding(Base):
    """Векторное представление сообщения (768-мерный эмбеддинг) для гибридного поиска /ask."""

    __tablename__ = "message_embeddings"

    message_id: Mapped[int] = mapped_column(
        ForeignKey("messages.id", ondelete="CASCADE"), primary_key=True
    )
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    embedding: Mapped[list[float]] = mapped_column(Vector(768), nullable=False)
