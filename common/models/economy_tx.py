from __future__ import annotations

from sqlalchemy import BigInteger
from sqlalchemy import DateTime
from sqlalchemy import ForeignKey
from sqlalchemy import String
from sqlalchemy import Text
from sqlalchemy import func
from sqlalchemy.orm import Mapped
from sqlalchemy.orm import mapped_column

from common.db.base import Base


class EconomyTx(Base):
    """Append-only журнал денежных операций (ECON-03) — строки никогда не UPDATE/DELETE.

    user_id NULLABLE: NULL означает банковскую сторону операции (комиссия/минт/сток).
    amount — знаковое значение (+ кредит, - дебет).
    Индексы и частичный UNIQUE(chat_id, ref_id, kind) создаются в миграции 0004,
    не здесь (фундамент идемпотентности ECON-03).
    """

    __tablename__ = "economy_tx"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    amount: Mapped[int] = mapped_column(BigInteger, nullable=False)
    kind: Mapped[str] = mapped_column(String(40), nullable=False)
    ref_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[DateTime] = mapped_column(DateTime, server_default=func.now())
