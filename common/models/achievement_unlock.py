from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger
from sqlalchemy import DateTime
from sqlalchemy import ForeignKey
from sqlalchemy import Integer
from sqlalchemy import String
from sqlalchemy import UniqueConstraint
from sqlalchemy import func
from sqlalchemy.orm import Mapped
from sqlalchemy.orm import mapped_column

from common.db.base import Base


class AchievementUnlock(Base):
    """Персистентный лог разблокированных ачивок (QUEST-02) — одна строка
    НАВСЕГДА на (chat_id, user_id, achievement_key), в отличие от
    QuestCompletion (common/models/quest_completion.py), где день входит в
    ключ уникальности.

    bonus_levels/reward_amount — снапшот каталога НА МОМЕНТ разблокировки
    (та же идея, что EconomyTx.amount фиксирует сумму на момент транзакции,
    common/models/economy_tx.py) — если каталог ачивок в bot/services/
    achievements_service.py позже перебалансируют, уже выданные бонусы
    участников задним числом не меняются.
    """

    __tablename__ = "achievement_unlocks"
    __table_args__ = (
        UniqueConstraint(
            "chat_id", "user_id", "achievement_key", name="uq_achievement_unlock"
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    achievement_key: Mapped[str] = mapped_column(String(32), nullable=False)
    bonus_levels: Mapped[int] = mapped_column(Integer, nullable=False)
    reward_amount: Mapped[int] = mapped_column(BigInteger, nullable=False)
    unlocked_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
