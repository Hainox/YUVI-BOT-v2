"""Ежедневный Arena-дайджест и публикация лучшего боя.

В 23:00 МСК сначала фиксируются только что рассчитанные номинации, затем
публикуется текстовая сводка. MP4 отправляется отдельным сообщением и никогда
не блокирует текст: отсутствие ffmpeg, битый replay или задержка рендера
оставляют дайджест доступным. ``ArenaDigestPublication`` предотвращает
повторную отправку при обычном повторном запуске job; граница Telegram API
остаётся at-least-once (краш между send и DB update может дать один дубль).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from typing import Any

from aiogram import Bot
from aiogram.types import FSInputFile
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import settings
from bot.services.arena_award_service import settle_daily_awards
from bot.services.arena_daily_awards_service import compute_daily_nominations
from bot.services.arena_daily_awards_service import daily_bounds
from bot.services.arena_daily_awards_service import nominate_daily_awards
from bot.services.arena_replay_service import event_payload
from bot.services.arena_replay_service import render_match_replay
from common.db.session import SessionLocal
from common.models.arena import ArenaDailyAward
from common.models.arena import ArenaDigestPublication
from common.models.arena import ArenaMatch
from common.models.arena import ArenaMatchEvent
from common.models.user import User

logger = logging.getLogger(__name__)
MSK = timezone(timedelta(hours=3), "MSK")
_JOB_ID = "arena_daily_digest"


@dataclass(frozen=True)
class ArenaDigest:
    text: str
    best_match_id: int | None
    day_msk: date


def _name(user: User | None, user_id: int | None) -> str:
    if user is None:
        return f"Игрок {user_id}" if user_id is not None else "—"
    return f"@{user.username}" if user.username else (user.first_name or f"Игрок {user.id}")


def _winner_line(match: ArenaMatch, users: dict[int, User]) -> str:
    if match.match_result == "draw":
        return "ничья"
    winner = users.get(match.winner_id) if match.winner_id is not None else None
    return _name(winner, match.winner_id)


async def build_arena_digest(
    session: AsyncSession, chat_id: int, day_msk: date
) -> ArenaDigest:
    """Собирает сводку только по завершённым авторитетным Arena-матчам."""
    start, end = daily_bounds(day_msk)
    result = await session.execute(
        select(ArenaMatch)
        .where(
            ArenaMatch.chat_id == chat_id,
            ArenaMatch.status == "finished",
            ArenaMatch.match_result.is_not(None),
            ArenaMatch.resolved_at >= start,
            ArenaMatch.resolved_at < end,
        )
        .order_by(ArenaMatch.id.asc())
    )
    matches = list(result.scalars().all())
    user_ids = {
        user_id
        for match in matches
        for user_id in (match.creator_id, match.opponent_id, match.winner_id)
        if user_id is not None
    }
    users: dict[int, User] = {}
    if user_ids:
        users = {
            user.id: user
            for user in (
                await session.execute(select(User).where(User.id.in_(user_ids)))
            ).scalars().all()
        }

    events: list[ArenaMatchEvent] = []
    if matches:
        events = list(
            (
                await session.execute(
                    select(ArenaMatchEvent)
                    .where(ArenaMatchEvent.match_id.in_([match.id for match in matches]))
                    .order_by(ArenaMatchEvent.match_id.asc(), ArenaMatchEvent.sequence.asc())
                )
            ).scalars().all()
        )
    events_by_match: dict[int, list[ArenaMatchEvent]] = {}
    for event in events:
        events_by_match.setdefault(event.match_id, []).append(event)

    wins = sum(1 for match in matches if match.match_result == "win")
    draws = sum(1 for match in matches if match.match_result == "draw")
    knockouts = [match for match in matches if match.result_reason == "knockout"]
    fastest = min(knockouts, key=lambda match: (match.phase_count, match.id), default=None)
    nominations = await compute_daily_nominations(session, chat_id, day_msk)
    best = next((item for item in nominations if item["key"] == "best_fight"), None)
    best_match_id = (best or {}).get("details", {}).get("match_id") if best else None

    lines = [
        "⚔️ ARENA — ИТОГИ ДНЯ",
        f"{day_msk:%d.%m.%Y} · боёв: {len(matches)}",
        f"Победы: {wins} · ничьи: {draws} · нокауты: {len(knockouts)}",
    ]
    if fastest is not None:
        lines.append(
            f"⚡ Самый быстрый нокаут: {_winner_line(fastest, users)} · фаза {fastest.phase_count}"
        )
    else:
        lines.append("⚡ Самый быстрый нокаут: пока без нокаутов")
    if best_match_id is not None:
        lines.append(f"🏆 Лучший бой дня: матч #{best_match_id}")
    else:
        lines.append("🏆 Лучший бой дня: подходящего боя нет")

    award_rows = list(
        (
            await session.execute(
                select(ArenaDailyAward).where(
                    ArenaDailyAward.chat_id == chat_id,
                    ArenaDailyAward.award_date == day_msk,
                )
            )
        ).scalars().all()
    )
    awards_by_key = {row.award_key: row for row in award_rows}

    lines.append("")
    lines.append("🎖 Номинации")
    for nomination in nominations:
        winner_id = nomination.get("winner_user_id")
        if winner_id is None:
            continue
        award = awards_by_key.get(nomination["key"])
        if award is None or award.settlement_status in {"nominated", "pending"}:
            payout_text = f"ожидает выплат: {nomination['reward']} ювиков"
        elif award.settlement_status == "partial":
            payout_text = f"выдано {award.paid_amount}/{award.reward_amount} ювиков"
        elif award.settlement_status == "paid":
            payout_text = f"+{award.paid_amount} ювиков"
        else:
            payout_text = "выплата временно недоступна"
        lines.append(
            f"• {nomination['label']}: {_name(users.get(winner_id), winner_id)} "
            f"({payout_text})"
        )
    if not any(item.get("winner_user_id") is not None for item in nominations):
        lines.append("• Награды не назначены — завершённых боёв с результатом нет.")

    # Keep this data path explicit for the renderer: build_arena_digest never
    # serializes events into the public text and never exposes bets/payouts.
    _ = events_by_match
    return ArenaDigest("\n".join(lines), best_match_id, day_msk)


async def _publication(session: AsyncSession, chat_id: int, day_msk: date, best_match_id: int | None) -> ArenaDigestPublication:
    # Upsert before FOR UPDATE: two bot processes must not race on the unique
    # (chat_id, digest_date) key and abort the whole digest transaction.
    await session.execute(
        pg_insert(ArenaDigestPublication)
        .values(
            chat_id=chat_id,
            digest_date=day_msk,
            best_match_id=best_match_id,
        )
        .on_conflict_do_nothing(
            index_elements=["chat_id", "digest_date"]
        )
    )
    row = (
        await session.execute(
            select(ArenaDigestPublication)
            .where(
                ArenaDigestPublication.chat_id == chat_id,
                ArenaDigestPublication.digest_date == day_msk,
            )
            .with_for_update()
        )
    ).scalar_one()
    if row.best_match_id is None and best_match_id is not None:
        row.best_match_id = best_match_id
    return row


async def publish_arena_digest(bot: Bot, *, day_msk: date | None = None) -> None:
    """Отправляет текст вовремя, а replay — отдельно best effort."""
    day_msk = day_msk or datetime.now(MSK).date()
    chat_id = settings.chat_id
    async with SessionLocal() as session:
        # Nomination is self-healing if the earlier 21:50 job was missed.
        # Settlement stays in this transaction, so the digest never claims a
        # reward was paid before the balance/ledger writes are committed.
        await nominate_daily_awards(session, chat_id, day_msk)
        await settle_daily_awards(session, chat_id, day_msk)
        digest = await build_arena_digest(session, chat_id, day_msk)
        publication = await _publication(session, chat_id, day_msk, digest.best_match_id)
        await session.commit()
        text_message_id = publication.text_message_id

    if text_message_id is None:
        try:
            message = await bot.send_message(chat_id, digest.text)
        except Exception:
            logger.exception("Arena digest text send failed chat_id=%s date=%s", chat_id, day_msk)
            return
        async with SessionLocal() as session:
            publication = await _publication(session, chat_id, day_msk, digest.best_match_id)
            if publication.text_message_id is None:
                publication.text_message_id = message.message_id
            await session.commit()

    async with SessionLocal() as session:
        publication = await _publication(session, chat_id, day_msk, digest.best_match_id)
        if publication.replay_status in {"sent", "skipped"}:
            return
        if publication.best_match_id is None:
            publication.replay_status = "skipped"
            await session.commit()
            return
        match = await session.get(ArenaMatch, publication.best_match_id)
        if match is None:
            publication.replay_status = "skipped"
            await session.commit()
            return
        events = list(
            (
                await session.execute(
                    select(ArenaMatchEvent)
                    .where(ArenaMatchEvent.match_id == match.id)
                    .order_by(ArenaMatchEvent.sequence.asc())
                )
            ).scalars().all()
        )
        # Do not keep a DB transaction open while ffmpeg/Telegram works.
        await session.commit()

    if not events:
        async with SessionLocal() as session:
            publication = await _publication(session, chat_id, day_msk, digest.best_match_id)
            publication.replay_status = "skipped"
            await session.commit()
        return

    await _send_replay(
        bot,
        chat_id,
        day_msk,
        match,
        events,
        digest.best_match_id,
    )


async def _send_replay(bot: Bot, chat_id: int, day_msk: date, match: ArenaMatch, events: list[ArenaMatchEvent], best_match_id: int) -> None:
    """Isolated helper so temporary MP4 lifetime covers Telegram upload."""
    try:
        async with render_match_replay([event_payload(event) for event in events], match) as path:
            message = await bot.send_video(
                chat_id,
                FSInputFile(path),
                caption=f"🎬 MP4 replay лучшего боя дня · матч #{best_match_id}",
                supports_streaming=True,
            )
    except Exception as exc:
        logger.warning("Arena replay send failed match_id=%s: %s", best_match_id, exc, exc_info=True)
        status = "failed"
        message_id = None
    else:
        status = "sent"
        message_id = message.message_id

    async with SessionLocal() as session:
        publication = await _publication(session, chat_id, day_msk, best_match_id)
        publication.replay_status = status
        if message_id is not None:
            publication.replay_message_id = message_id
        await session.commit()


def register_daily_digest(scheduler, bot: Bot, *, hour: int = 23, minute: int = 0) -> None:
    async def _job() -> None:
        try:
            await publish_arena_digest(bot)
        except Exception:
            logger.exception("Arena daily digest job failed")

    scheduler.add_job(
        _job,
        "cron",
        hour=hour,
        minute=minute,
        timezone=MSK,
        coalesce=True,
        max_instances=1,
        misfire_grace_time=3600,
        id=_JOB_ID,
        replace_existing=True,
    )
