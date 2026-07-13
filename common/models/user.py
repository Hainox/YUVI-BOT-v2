from __future__ import annotations

from sqlalchemy import BigInteger
from sqlalchemy import DateTime
from sqlalchemy import String
from sqlalchemy import func
from sqlalchemy.orm import Mapped
from sqlalchemy.orm import mapped_column

from common.db.base import Base


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    first_name: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[DateTime] = mapped_column(DateTime, server_default=func.now())

