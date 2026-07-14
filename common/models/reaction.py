from __future__ import annotations

from sqlalchemy import BigInteger
from sqlalchemy import DateTime
from sqlalchemy import ForeignKey
from sqlalchemy import String
from sqlalchemy import UniqueConstraint
from sqlalchemy import func
from sqlalchemy.orm import Mapped
from sqlalchemy.orm import mapped_column

from common.db.base import Base


class Reaction(Base):
    """Current-state snapshot реакции на (message, actor) — не append-only event log (Assumption A3)."""

    __tablename__ = "reactions"
    __table_args__ = (
        UniqueConstraint("message_id", "actor_user_id", "emoji", name="uq_reaction_msg_actor_emoji"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    message_id: Mapped[int] = mapped_column(ForeignKey("messages.id"), nullable=False)
    actor_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    emoji: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[DateTime] = mapped_column(DateTime, server_default=func.now())
