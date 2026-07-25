from __future__ import annotations

from datetime import date
from datetime import datetime

from sqlalchemy import BigInteger
from sqlalchemy import Date
from sqlalchemy import DateTime
from sqlalchemy import ForeignKey
from sqlalchemy import String
from sqlalchemy import UniqueConstraint
from sqlalchemy import func
from sqlalchemy.orm import Mapped
from sqlalchemy.orm import mapped_column

from common.db.base import Base


class QuestCompletion(Base):
    """Персистентный лог выполненных ежедневных квестов (QUEST-01).

    UNIQUE(chat_id, user_id, quest_key, day_msk) — день входит в ключ, поэтому
    новый MSK-день структурно всегда свежая строка (форма daily_picks,
    common/models/daily_pick.py) — явный сброс не нужен. Эта же таблица служит
    ДВУМ целям: идемпотентный гейт (quests_service.claim_quests вставляет
    ровно один раз в день на квест через on_conflict_do_nothing) И источник
    истины для cumulative-счётчика выполненных квестов, который
    clicker_service.get_effective_max_level читает, чтобы поднять потолок
    фермы (QUEST-01/02) — счётчик персистентный (не производный запрос к
    daily_stats "на лету"), чтобы разблокированный уровень фермы никогда не
    уменьшался задним числом.
    """

    __tablename__ = "quest_completions"
    __table_args__ = (
        UniqueConstraint(
            "chat_id", "user_id", "quest_key", "day_msk", name="uq_quest_completion"
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    quest_key: Mapped[str] = mapped_column(String(32), nullable=False)
    day_msk: Mapped[date] = mapped_column(Date, nullable=False)
    reward_amount: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
