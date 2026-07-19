"""Ежедневная лотерея `/yuvi` (LOTTERY-01) — «Yuvi_Yuvi дня»: случайный
участник из вчерашних активных, идемпотентно по MSK-дню поверх общего
`daily_pick_service.get_or_set_pick` (kind='lottery', тот же примитив, что
жертва дня, план 05-04) — ноль дублирования get-or-set-логики.

Announcement-only (D-10): этот модуль НЕ зовёт economy_service (нет приза) и
НЕ зовёт tag_service (реальный Telegram custom_title — только у жертвы дня),
лишь фиксирует пик и анонсирует победителя.

Сброс 00:00 МСК (`register_daily_reset`) — UX-, не correctness-критичен
(Pitfall 4): смена `day_msk` в UNIQUE-ключе `get_or_set_pick` сама по себе
даёт свежий пик на новый день, даже если планировщик не сработал.
`expires_at` строки = конец текущего MSK-дня — тот же safety-net (Success
Criterion 3).
"""

from __future__ import annotations

import html
import logging
from datetime import datetime
from datetime import time
from datetime import timedelta

from aiogram import Bot
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import settings
from bot.services import daily_pick_service
from common.db.session import SessionLocal
from common.models.daily_stat import DailyStat
from common.models.user import User

logger = logging.getLogger(__name__)


async def _yesterday_candidates(session: AsyncSession, chat_id: int) -> list[int]:
    """Кандидаты в Yuvi_Yuvi дня — ВЧЕРАШНИЕ активные участники
    (distinct daily_stats.user_id за stat_date=вчера относительно
    daily_pick_service._today_msk()), не все подряд."""
    yesterday = daily_pick_service._today_msk() - timedelta(days=1)
    result = await session.execute(
        select(DailyStat.user_id)
        .where(DailyStat.chat_id == chat_id, DailyStat.stat_date == yesterday)
        .distinct()
    )
    return [row[0] for row in result.all()]


async def run_lottery(session: AsyncSession, chat_id: int) -> dict:
    """Выбирает (или возвращает уже выбранного) Yuvi_Yuvi дня из вчерашних
    активных участников. Announcement-only — НИКАКИХ вызовов
    economy_service/tag_service (D-10). Коммитит.

    Возвращает {winner, is_new, day_msk}; если вчера в чате не было ни
    одного активного участника — {winner: None, is_new: False, day_msk: None}.
    """
    candidates = await _yesterday_candidates(session, chat_id)
    if not candidates:
        return {"winner": None, "is_new": False, "day_msk": None}

    day_msk = daily_pick_service._today_msk()
    # Safety-net (Pitfall 4/Success Criterion 3): конец текущего MSK-дня —
    # если 00:00-джоб не сработал, следующий вызов всё равно попадёт в новый
    # day_msk и выберет свежего Yuvi_Yuvi.
    expires_at = datetime.combine(day_msk, time(23, 59, 59))

    winner, is_new = await daily_pick_service.get_or_set_pick(
        session, chat_id, kind="lottery", candidates=candidates, expires_at=expires_at
    )
    await session.commit()

    return {"winner": winner, "is_new": is_new, "day_msk": day_msk}


# --- register_daily_reset (APScheduler, форма awards_service.register_daily_autopost) --

_RESET_JOB_ID = "lottery_daily_reset"


def register_daily_reset(scheduler: AsyncIOScheduler, bot: Bot) -> None:
    """Регистрирует проактивный сброс/анонс Yuvi_Yuvi дня 00:00 МСК (cron), по
    образцу `awards_service.register_daily_autopost`/
    `tag_service.register_title_expiry`: своя `SessionLocal`, broad-except —
    тик обязан пережить любую ошибку и не уронить планировщик.
    `coalesce+max_instances=1` — пропущенные срабатывания не постят несколько
    раз (T-05-07/T-02-22 прецедент). Чисто UX: correctness сброса и так
    обеспечен day_msk внутри get_or_set_pick (Pitfall 4)."""

    async def _job() -> None:
        async with SessionLocal() as session:
            try:
                result = await run_lottery(session, settings.chat_id)
                if result["winner"] is None:
                    return
                name = (
                    await session.execute(
                        select(User.first_name).where(User.id == result["winner"])
                    )
                ).scalar_one_or_none() or str(result["winner"])
                await bot.send_message(
                    settings.chat_id,
                    f"🎲 Yuvi_Yuvi дня: <b>{html.escape(name)}</b>",
                    parse_mode="HTML",
                )
            except Exception:  # noqa: BLE001 - тик обязан пережить любую ошибку
                logger.exception("lottery_daily_reset: тик упал")

    scheduler.add_job(
        _job,
        "cron",
        hour=0,
        minute=0,
        timezone=daily_pick_service.MSK,
        coalesce=True,
        max_instances=1,
        misfire_grace_time=3600,
        id=_RESET_JOB_ID,
        replace_existing=True,
    )
