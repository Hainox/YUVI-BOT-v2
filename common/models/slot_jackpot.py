from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger
from sqlalchemy import DateTime
from sqlalchemy import func
from sqlalchemy.orm import Mapped
from sqlalchemy.orm import mapped_column

from common.db.base import Base


class SlotJackpot(Base):
    """Прогрессивный джекпот слота, отдельный на каждый чат (CASINO-06).

    `pool` — текущая накопленная сумма (растёт на долю каждой ставки слота,
    сбрасывается на settings.slot_jackpot_seed при выигрыше). Одна строка на
    chat_id — читается/меняется под FOR UPDATE в jackpot_service, чтобы
    конкурентные спины одного чата не гонялись за одно и то же обновление.
    """

    __tablename__ = "slot_jackpots"

    chat_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    pool: Mapped[int] = mapped_column(BigInteger, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
