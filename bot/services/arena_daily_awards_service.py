"""Безопасный foundation ежедневных Arena-наград.

Модуль фиксирует детерминированный план наград в ``ArenaDailyAward``.
Фактическая атомарная выдача выполняется ``arena_award_service`` после
номинации: сначала Arena fund, затем fallback в chat bank.

Метрики считаются только по завершённым матчам и авторитетным
``ArenaMatchEvent``. Redis и клиентские данные не являются источником
результата. Повторный запуск защищён unique ``fund_ledger_key``.
"""

from __future__ import annotations

from collections import Counter
from datetime import date
from datetime import datetime
from datetime import time
from datetime import timedelta
from datetime import timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from common.models.arena import ArenaDailyAward
from common.models.arena import ArenaMatch
from common.models.arena import ArenaMatchEvent


DAILY_REWARDS: dict[str, int] = {
    "best_fight": 1_000,
    "fastest_knockout": 500,
    "best_comeback": 500,
    "best_counter": 300,
    "most_active": 300,
}

DAILY_LABELS: dict[str, str] = {
    "best_fight": "Лучший бой",
    "fastest_knockout": "Самый быстрый нокаут",
    "best_comeback": "Лучший камбэк",
    "best_counter": "Лучший контр",
    "most_active": "Самый активный игрок",
}

_ARENA_TIMEZONE = timezone(timedelta(hours=3), "MSK")
_DAILY_JOB_ID = "arena_daily_award_nomination"


def daily_bounds(day_msk: date) -> tuple[datetime, datetime]:
    """Возвращает границы календарного дня Arena в МСК."""
    start = datetime.combine(day_msk, time.min, tzinfo=_ARENA_TIMEZONE)
    return start, start + timedelta(days=1)


def _event_stats(events: list[ArenaMatchEvent], match: ArenaMatch) -> dict[str, Any]:
    """Собирает воспроизводимые показатели одного боя из event ledger."""
    counters: Counter[int] = Counter()
    specials = 0
    total_damage = 0
    counter_first_phase: dict[int, int] = {}
    counter_damage: dict[int, int] = {}
    for event in events:
        payload = event.payload or {}
        player_damage = int(payload.get("player_damage", 0) or 0)
        opponent_damage = int(payload.get("opponent_damage", 0) or 0)
        total_damage += player_damage + opponent_damage
        if payload.get("player_special"):
            specials += 1
        if payload.get("opponent_special"):
            specials += 1
        if payload.get("player_counter"):
            counters[match.creator_id] += 1
            counter_first_phase.setdefault(match.creator_id, event.phase_index)
            counter_damage[match.creator_id] = max(counter_damage.get(match.creator_id, 0), player_damage)
        if payload.get("opponent_counter") and match.opponent_id is not None:
            counters[match.opponent_id] += 1
            counter_first_phase.setdefault(match.opponent_id, event.phase_index)
            counter_damage[match.opponent_id] = max(counter_damage.get(match.opponent_id, 0), opponent_damage)

    counter_count = sum(counters.values())
    counter_winner_id = None
    if counters:
        counter_winner_id = max(
            counters,
            key=lambda user_id: (
                counters[user_id],
                -counter_first_phase[user_id],
                -user_id,
            ),
        )
    # The formula is deliberately explicit and integer-only. It rewards
    # authored combat moments while keeping longer matches from winning only
    # because they produced more empty phases.
    fight_score = total_damage + counter_count * 25 + specials * 20 + min(match.phase_count, 30)
    return {
        "fight_score": fight_score,
        "counter_count": counter_count,
        "special_count": specials,
        "total_damage": total_damage,
        "counter_winner_id": counter_winner_id,
        "counter_damage": counter_damage.get(counter_winner_id, 0) if counter_winner_id is not None else 0,
    }


def _comeback_depth(events: list[ArenaMatchEvent], match: ArenaMatch) -> tuple[int, int | None]:
    """Return the largest deficit overcome by the eventual winner.

    Runtime events include post-phase HP snapshots. If an old match has no HP
    fields, it is intentionally not nominated as a comeback rather than
    guessing from incomplete history.
    """
    if match.winner_id is None or match.opponent_id is None:
        return 0, None
    winner_is_creator = match.winner_id == match.creator_id
    max_deficit = 0
    for event in events:
        payload = event.payload or {}
        player_hp = payload.get("player_hp")
        opponent_hp = payload.get("opponent_hp")
        if player_hp is None or opponent_hp is None:
            continue
        winner_hp, loser_hp = (
            (int(player_hp), int(opponent_hp))
            if winner_is_creator
            else (int(opponent_hp), int(player_hp))
        )
        max_deficit = max(max_deficit, loser_hp - winner_hp)
    return max_deficit, match.winner_id if max_deficit > 0 else None


def _empty_nomination(key: str) -> dict[str, Any]:
    return {
        "key": key,
        "label": DAILY_LABELS[key],
        "reward": DAILY_REWARDS[key],
        "winner_user_id": None,
        "metric_value": 0,
        "details": {},
    }


async def compute_daily_nominations(
    session: AsyncSession, chat_id: int, day_msk: date
) -> list[dict[str, Any]]:
    """Compute five deterministic daily nominations without financial writes."""
    start, end = daily_bounds(day_msk)
    matches_result = await session.execute(
        select(ArenaMatch)
        .where(
            ArenaMatch.chat_id == chat_id,
            ArenaMatch.status == "finished",
            ArenaMatch.resolved_at >= start,
            ArenaMatch.resolved_at < end,
        )
        .order_by(ArenaMatch.id.asc())
    )
    matches = [
        match
        for match in matches_result.scalars().all()
        # A both-disconnected/admin-refunded row may be ``finished`` without
        # a combat result. It must not create activity or combat awards.
        if match.match_result is not None
    ]
    nominations = {key: _empty_nomination(key) for key in DAILY_REWARDS}
    if not matches:
        return list(nominations.values())

    match_ids = [match.id for match in matches]
    events_result = await session.execute(
        select(ArenaMatchEvent)
        .where(ArenaMatchEvent.match_id.in_(match_ids))
        .order_by(ArenaMatchEvent.match_id.asc(), ArenaMatchEvent.sequence.asc())
    )
    events_by_match: dict[int, list[ArenaMatchEvent]] = {}
    for event in events_result.scalars().all():
        events_by_match.setdefault(event.match_id, []).append(event)

    scored: list[tuple[ArenaMatch, dict[str, Any]]] = [
        (match, _event_stats(events_by_match.get(match.id, []), match))
        for match in matches
    ]

    # ``best_fight`` is a single-recipient award. A draw has no winner, so do
    # not let a spectacular draw hide a payable win from this category.
    scored_with_winner = [item for item in scored if item[0].winner_id is not None]
    if scored_with_winner:
        best_match, best_stats = max(
            scored_with_winner,
            key=lambda item: (
                item[1]["fight_score"],
                item[1]["counter_count"],
                item[1]["special_count"],
                -item[0].id,
            ),
        )
        nominations["best_fight"].update(
            winner_user_id=best_match.winner_id,
            metric_value=best_stats["fight_score"],
            details={"match_id": best_match.id, **best_stats},
        )

    knockouts = [match for match in matches if match.result_reason == "knockout" and match.winner_id]
    if knockouts:
        fastest = min(knockouts, key=lambda match: (match.phase_count, match.id))
        nominations["fastest_knockout"].update(
            winner_user_id=fastest.winner_id,
            metric_value=fastest.phase_count,
            details={"match_id": fastest.id, "phase_count": fastest.phase_count},
        )

    comebacks = []
    for match, _stats in scored:
        depth, winner_id = _comeback_depth(events_by_match.get(match.id, []), match)
        if winner_id is not None:
            comebacks.append((depth, match.id, winner_id))
    if comebacks:
        depth, match_id, winner_id = max(comebacks, key=lambda row: (row[0], -row[1]))
        nominations["best_comeback"].update(
            winner_user_id=winner_id,
            metric_value=depth,
            details={"match_id": match_id, "deficit_hp": depth},
        )

    counter_candidates = [
        (stats["counter_count"], -match.id, stats["counter_winner_id"], match.id)
        for match, stats in scored
        if stats["counter_count"] > 0 and stats["counter_winner_id"] is not None
    ]
    if counter_candidates:
        count, _negative_match_id, winner_id, match_id = max(counter_candidates)
        nominations["best_counter"].update(
            winner_user_id=winner_id,
            metric_value=count,
            details={"match_id": match_id, "counter_count": count},
        )

    activity = Counter[int]()
    for match in matches:
        activity[match.creator_id] += 1
        if match.opponent_id is not None:
            activity[match.opponent_id] += 1
    if activity:
        winner_id, count = min(
            activity.items(), key=lambda item: (-item[1], item[0])
        )
        nominations["most_active"].update(
            winner_user_id=winner_id,
            metric_value=count,
            details={"matches": count},
        )

    return list(nominations.values())


async def nominate_daily_awards(
    session: AsyncSession, chat_id: int, day_msk: date
) -> list[ArenaDailyAward]:
    """Persist daily nomination rows; settlement и commit выполняет caller."""
    nominations = await compute_daily_nominations(session, chat_id, day_msk)
    rows = [
        {
            "chat_id": chat_id,
            "award_date": day_msk,
            "award_key": nomination["key"],
            "user_id": nomination["winner_user_id"],
            "reward_amount": nomination["reward"],
            "fund_ledger_key": f"arena:daily:{chat_id}:{day_msk}:{nomination['key']}",
            "details": {
                **nomination["details"],
                "metric_value": nomination["metric_value"],
                "status": "nominated",
            },
        }
        for nomination in nominations
        if nomination["winner_user_id"] is not None
    ]
    if rows:
        await session.execute(
            pg_insert(ArenaDailyAward)
            .values(rows)
            .on_conflict_do_nothing()
        )

    result = await session.execute(
        select(ArenaDailyAward)
        .where(
            ArenaDailyAward.chat_id == chat_id,
            ArenaDailyAward.award_date == day_msk,
        )
        .order_by(ArenaDailyAward.award_key.asc())
    )
    return list(result.scalars().all())


def register_daily_nomination(scheduler, *, hour: int = 21, minute: int = 50) -> None:
    """Register the 21:50 MSK nomination job before the shared 22:00 digest."""
    from bot.config import settings
    from common.db.session import SessionLocal

    async def _job() -> None:
        async with SessionLocal() as session:
            try:
                day_msk = datetime.now(_ARENA_TIMEZONE).date()
                await nominate_daily_awards(session, settings.chat_id, day_msk)
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    scheduler.add_job(
        _job,
        "cron",
        hour=hour,
        minute=minute,
        timezone=_ARENA_TIMEZONE,
        coalesce=True,
        max_instances=1,
        misfire_grace_time=3600,
        id=_DAILY_JOB_ID,
        replace_existing=True,
    )
