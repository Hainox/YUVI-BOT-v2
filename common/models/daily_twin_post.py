from __future__ import annotations

from datetime import date
from datetime import datetime

from sqlalchemy import BigInteger
from sqlalchemy import Date
from sqlalchemy import DateTime
from sqlalchemy import ForeignKey
from sqlalchemy import UniqueConstraint
from sqlalchemy import func
from sqlalchemy.orm import Mapped
from sqlalchemy.orm import mapped_column

from common.db.base import Base


class DailyTwinPost(Base):
    """Журнал постов "дневного двойника" (TWIN-03) — одна строка на КАЖДЫЙ
    пост, который бот отправил от имени выбранной на сегодня персоны
    (проактивный пост ИЛИ ответ на реплай). Нужна ТОЛЬКО для того, чтобы
    узнавать "это реплай на пост двойника?" (bot/handlers/daily_twin.py) —
    собственные сообщения бота НЕ попадают в `messages` вообще
    (bot/middleware/collector.py фильтрует is_bot ДО save_message), поэтому
    без отдельного журнала сматчить реплай было бы не по чему.

    UNIQUE(chat_id, telegram_message_id) — ключ для поиска по реплаю.
    day_msk — только для дешёвого COUNT() "сколько постов уже было сегодня"
    (daily_twin_service.count_todays_posts), не участвует в матчинге реплаев.
    """

    __tablename__ = "daily_twin_posts"
    __table_args__ = (UniqueConstraint("chat_id", "telegram_message_id", name="uq_daily_twin_post"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    telegram_message_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    target_user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    day_msk: Mapped[date] = mapped_column(Date, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
