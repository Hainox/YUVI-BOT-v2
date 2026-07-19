"""Жертва дня (VICTIM-01/02) — объединяет «тугосерю» (титул-позор) и `/fag`
(приз-only) в одну ежедневную механику: случайная жертва получает 228 ювиков
из банка чата (D-05), Telegram-титул «Жертва дня» на 24ч (D-10, через
`tag_service` — этот модуль сам НИКОГДА не зовёт Bot API напрямую) и
удвоенную комиссию перевода на 24ч (D-06).

Идемпотентно по MSK-дню (D-09): повторный `run_victim` в тот же день
возвращает ТУ ЖЕ жертву, не перевыбирает и не переплачивает — поверх общего
`daily_pick_service.get_or_set_pick` (kind='victim'). Новый MSK-день —
структурно свежая строка (Pitfall 4), явный сброс не нужен.

Дебафф резолвится КАЛЛЕРОМ (bot/handlers/economy.py) через `is_active_victim`
— `economy_service` остаётся ignorant про victim (05-RESEARCH.md Pattern 3).
"""

from __future__ import annotations

from datetime import datetime
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.services import daily_pick_service
from bot.services import economy_service
from common.models.daily_pick import DailyPick
from common.models.daily_stat import DailyStat

VICTIM_PRIZE = 228  # D-05: мемная сумма (та же серия, что AWARDS-02 228/322)
VICTIM_DEBUFF_HOURS = 24  # D-06
VICTIM_FEE_MULTIPLIER = 2.0  # D-06


async def _active_candidates(session: AsyncSession, chat_id: int) -> list[int]:
    """Кандидаты в жертвы — все участники чата, реально писавшие сообщения
    (distinct daily_stats.user_id), форма stats_service (никаких COUNT(*) по
    messages — RESEARCH.md Anti-Patterns)."""
    result = await session.execute(
        select(DailyStat.user_id).where(DailyStat.chat_id == chat_id).distinct()
    )
    return [row[0] for row in result.all()]


async def run_victim(session: AsyncSession, chat_id: int) -> dict:
    """Выбирает (или возвращает уже выбранную) жертву дня, платит приз из
    банка чата ТОЛЬКО при первом выборе (is_new=True). Коммитит.

    Возвращает {winner, is_new, prize, expires_at, day_msk}; если в чате ещё
    нет ни одного участника с daily_stats-активностью — {winner: None, ...}.
    """
    candidates = await _active_candidates(session, chat_id)
    if not candidates:
        return {"winner": None, "is_new": False, "prize": 0, "expires_at": None, "day_msk": None}

    day_msk = daily_pick_service._today_msk()
    expires_at = datetime.utcnow() + timedelta(hours=VICTIM_DEBUFF_HOURS)

    winner, is_new = await daily_pick_service.get_or_set_pick(
        session, chat_id, kind="victim", candidates=candidates, expires_at=expires_at
    )

    prize = 0
    if is_new:
        prize = await economy_service.pay_from_bank(
            session,
            chat_id,
            winner,
            VICTIM_PRIZE,
            kind="victim_prize",
            ref_id=f"victim:{chat_id}:{day_msk}",
        )

    # expires_at строки — источник истины (при is_new=False только что
    # вычисленный expires_at выше никуда не записывался).
    stored_expires_at = (
        await session.execute(
            select(DailyPick.expires_at).where(
                DailyPick.chat_id == chat_id,
                DailyPick.kind == "victim",
                DailyPick.day_msk == day_msk,
            )
        )
    ).scalar_one()

    await session.commit()

    return {
        "winner": winner,
        "is_new": is_new,
        "prize": prize,
        "expires_at": stored_expires_at,
        "day_msk": day_msk,
    }


async def is_active_victim(session: AsyncSession, chat_id: int, user_id: int) -> bool:
    """True, если user_id — активная жертва дня прямо сейчас (kind='victim',
    day_msk=сегодня, expires_at > now) — используется /transfer для резолва
    fee_multiplier (05-RESEARCH.md Pattern 3, T-05-12)."""
    today = daily_pick_service._today_msk()
    now = datetime.utcnow()
    exists = (
        await session.execute(
            select(DailyPick.id).where(
                DailyPick.chat_id == chat_id,
                DailyPick.kind == "victim",
                DailyPick.day_msk == today,
                DailyPick.winner_user_id == user_id,
                DailyPick.expires_at.isnot(None),
                DailyPick.expires_at > now,
            )
        )
    ).scalar_one_or_none()
    return exists is not None
