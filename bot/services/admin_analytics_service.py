"""Read-only агрегации для админ-панели (CASINO-03, D-03).

Три чистые SELECT ... GROUP BY функции над уже существующими таблицами
(`casino_games`, `economy_tx`) — та же форма, что `economy_service.
get_chat_summary`/`get_leaderboard`: без ORM-мутаций, без `session.commit()`
(read-only). Никакой новой инфраструктуры трекинга (D-03) — DAU выводится
из `casino_games.created_at`, не из отдельной "app_events"-таблицы.

Знаковая конвенция `EconomyTx.amount` (04.3-RESEARCH.md Pitfall 2):
`casino_bet` user-side нога отрицательная (списание), bank-side
(`user_id IS NULL`) положительная; `casino_payout` — зеркально. `get_turnover`
разделяет их через `user_id.is_not(None)`/`user_id.is_(None)` — НЕ наивный
`SUM(amount)` по всем строкам сразу.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import func
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from common.models.casino_game import CasinoGame
from common.models.economy_tx import EconomyTx


async def get_game_popularity(session: AsyncSession, chat_id: int, since: datetime) -> list[dict]:
    """Количество раундов по типам игр за период, по убыванию."""
    stmt = (
        select(CasinoGame.game, func.count().label("rounds"))
        .where(CasinoGame.chat_id == chat_id, CasinoGame.created_at >= since)
        .group_by(CasinoGame.game)
        .order_by(func.count().desc())
    )
    result = await session.execute(stmt)
    return [{"game": row.game, "rounds": row.rounds} for row in result.all()]


async def get_turnover(session: AsyncSession, chat_id: int, since: datetime) -> dict:
    """Оборот ювиков за период: ставки (bets_placed) + комиссия банка
    (bank_commission), реализованная как net-сумма bank-side ног
    casino_bet/casino_payout (Pitfall 2)."""
    bets_placed = (
        await session.execute(
            select(func.coalesce(func.sum(func.abs(EconomyTx.amount)), 0)).where(
                EconomyTx.chat_id == chat_id,
                EconomyTx.kind == "casino_bet",
                EconomyTx.user_id.is_not(None),
                EconomyTx.created_at >= since,
            )
        )
    ).scalar_one()

    bank_commission = (
        await session.execute(
            select(func.coalesce(func.sum(EconomyTx.amount), 0)).where(
                EconomyTx.chat_id == chat_id,
                EconomyTx.kind.in_(("casino_bet", "casino_payout")),
                EconomyTx.user_id.is_(None),
                EconomyTx.created_at >= since,
            )
        )
    ).scalar_one()

    return {"bets_placed": int(bets_placed), "bank_commission": int(bank_commission)}


async def get_active_players(session: AsyncSession, chat_id: int, since: datetime) -> list[dict]:
    """DAU-style: число уникальных участников, сыгравших раунд, по дням."""
    stmt = (
        select(
            func.date(CasinoGame.created_at).label("day"),
            func.count(func.distinct(CasinoGame.user_id)).label("active_players"),
        )
        .where(CasinoGame.chat_id == chat_id, CasinoGame.created_at >= since)
        .group_by(func.date(CasinoGame.created_at))
        .order_by(func.date(CasinoGame.created_at))
    )
    result = await session.execute(stmt)
    return [{"day": str(row.day), "active_players": row.active_players} for row in result.all()]
