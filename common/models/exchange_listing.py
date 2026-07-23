from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger
from sqlalchemy import DateTime
from sqlalchemy import ForeignKey
from sqlalchemy import String
from sqlalchemy import func
from sqlalchemy.orm import Mapped
from sqlalchemy.orm import mapped_column

from common.db.base import Base


class ExchangeListing(Base):
    """P2P-биржа ювиков (EXCHANGE-01) — продавец выставляет ювики на продажу
    со свободным текстовым описанием желаемой оплаты (`want_description`);
    сама оплата происходит ВНЕ бота, между двумя людьми. Бот эскроирует
    ТОЛЬКО ювик-сторону сделки — см. докстринг bot/services/exchange_service.py
    про дизайн escrow -> claim -> seller confirms и его границы.

    status: open -> claimed -> fulfilled, либо open/claimed -> cancelled
    (форма duels.status/markets.status).

    item_type/gacha_char_id — колонки уже в схеме на будущее (гача-карты),
    но сам флоу карточных листингов НЕ реализован в этой фазе (гача выключена
    флагом GACHA_DISABLED в miniapp) — см. exchange_service.create_listing.
    """

    __tablename__ = "exchange_listings"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    seller_user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    yuvik_amount: Mapped[int] = mapped_column(BigInteger, nullable=False)
    want_description: Mapped[str] = mapped_column(String(300), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="open")
    item_type: Mapped[str] = mapped_column(String(16), nullable=False, default="yuvik")
    gacha_char_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    claimed_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
