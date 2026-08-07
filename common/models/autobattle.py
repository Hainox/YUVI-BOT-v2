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


class AutoBattle(Base):
    """PvP-автобитва гача-коллекциями на ставку (GACHA-06, "по образцу" Duel —
    читай `bot/services/duel_service.py`'s докстринг за полным разбором формы,
    здесь только то, что реально отличается).

    В отличие от Duel (честный 50/50 coinflip), исход взвешен суммарной
    "силой" владеемой гача-коллекции каждой стороны (`autobattle_service.
    team_power`) — сильнее коллекция не гарантирует победу (вероятность
    зажата в [5%, 95%], см. `autobattle_service.win_probability`), но реально
    на неё влияет. `challenger_power`/`opponent_power` — снапшот силы на
    момент резолва (`accept_autobattle`), для отображения результата и
    аудита; сама сила не хранится нигде ПОСТОЯННО (пересчитывается из
    `GachaCollection` каждый раз заново, см. докстринг team_power — коллекция
    может измениться между вызовом и резолвом, что и задумано: сила всегда
    актуальная на момент боя, а не замороженная на момент вызова).

    status следует форме Duel.status: pending -> accepted(->resolved)/
    declined/cancelled — фактически сразу resolved на accept (синхронный
    резолв, как у Duel, без промежуточного "боя").
    """

    __tablename__ = "autobattles"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    challenger_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    opponent_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    stake: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    winner_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    loser_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    challenger_power: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    opponent_power: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    fee: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
