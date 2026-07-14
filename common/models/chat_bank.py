from __future__ import annotations

from sqlalchemy import BigInteger
from sqlalchemy.orm import Mapped
from sqlalchemy.orm import mapped_column

from common.db.base import Base


class ChatBank(Base):
    """Банк чата — одна строка на чат (chat_id сам является PK).

    Намеренно БЕЗ CheckConstraint на balance >= 0: казино Фазы 4 легально
    уводит банк в минус (house edge P&L), см. RESEARCH.md «Negative balance
    prevention».
    """

    __tablename__ = "chat_bank"

    chat_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    balance: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
