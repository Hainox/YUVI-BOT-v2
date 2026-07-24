from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger
from sqlalchemy import DateTime
from sqlalchemy import String
from sqlalchemy import Text
from sqlalchemy import func
from sqlalchemy.orm import Mapped
from sqlalchemy.orm import mapped_column

from common.db.base import Base


class ChangelogEntry(Base):
    """Запись «Что нового» (WHATSNEW-01, запрошено 2026-07-24) — обновления и
    планы разработки, публикуемые владельцем бота через `/post_update`
    (bot/handlers/owner.py). Глобальные, не привязаны к конкретному chat_id —
    один продукт, одна лента новостей на всех.
    """

    __tablename__ = "changelog_entries"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    body: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
