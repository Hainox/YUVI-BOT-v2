from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger
from sqlalchemy import DateTime
from sqlalchemy import ForeignKey
from sqlalchemy import String
from sqlalchemy import func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped
from sqlalchemy.orm import mapped_column

from common.db.base import Base


class CasinoGame(Base):
    """Раунд казино (coinflip/dice/roulette/blackjack/slots) — фундамент 04.1.

    idem_key — replay-ключ повторного запроса клиента; частичный UNIQUE
    (user_id, idem_key) WHERE idem_key IS NOT NULL создаётся в миграции 0005
    (Pattern 4), не здесь. Намеренно БЕЗ CHECK на bet/payout — house-edge
    P&L казино не блокируется на уровне модели (форма chat_bank из Фазы 3).
    """

    __tablename__ = "casino_games"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    game: Mapped[str] = mapped_column(String(16), nullable=False)
    bet: Mapped[int] = mapped_column(BigInteger, nullable=False)
    payout: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    outcome: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    state: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="settled")
    idem_key: Mapped[str | None] = mapped_column(String(80), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
