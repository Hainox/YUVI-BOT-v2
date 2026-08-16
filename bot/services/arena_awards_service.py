"""Безопасный фундамент недельных Arena-срезов и наград.

Этот модуль намеренно разделяет три операции:

* snapshot фиксирует общий Arena leaderboard за период;
* nominate_weekly_awards выбирает активных участников из уже зафиксированного
  snapshot и создаёт записи-планировщики наград; один пользователь может быть
  номинирован на один weekly rank только один раз глобально;
* weekly snapshot job перед коммитом вызывает атомарный settlement с
  fallback Arena fund -> chat bank, а retry job продолжает partial/pending rows.

Номинация и settlement не коммитят сами: вызывающий job/request владеет
границей транзакции.
"""

from __future__ import annotations

from datetime import date
from datetime import datetime
from datetime import time
from datetime import timedelta
from datetime import timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.dialects.postgresql import insert as pg_insert

from common.models.arena import ArenaLeaderboardSnapshot
from common.models.arena import ArenaMatch
from common.models.arena import ArenaProfile
from common.models.arena import ArenaWeeklyAward


async def _settle_weekly_awards(session: AsyncSession, chat_id: int, period_start: date) -> None:
    from bot.services.arena_award_service import settle_weekly_awards

    await settle_weekly_awards(session, chat_id, period_start)


WEEKLY_REWARDS: dict[int, int] = {
    1: 25_000,
    2: 10_000,
    3: 10_000,
}
for _rank in range(4, 11):
    WEEKLY_REWARDS[_rank] = 5_000

_WEEKLY_SNAPSHOT_JOB_ID = "arena_weekly_leaderboard_snapshot"
# Moscow has used a fixed UTC+03:00 offset since 2014; a fixed offset keeps
# period calculation usable in minimal API/test images without tzdata.
_ARENA_TIMEZONE = timezone(timedelta(hours=3), "MSK")


def week_start(value: date) -> date:
    """Возвращает понедельник ISO-недели для ``value``."""
    return value - timedelta(days=value.weekday())


def _period_bounds(period_start: date) -> tuple[datetime, datetime]:
    """Возвращает границы недели в той же зоне, что и Arena scheduler.

    Периоды Arena считаются по МСК. Нельзя смешивать их с UTC: это создавало
    бы окно до трёх часов, в котором матчи попадали в соседнюю неделю.
    """
    normalized = week_start(period_start)
    start = datetime.combine(normalized, time.min, tzinfo=_ARENA_TIMEZONE)
    return start, start + timedelta(days=7)


def weekly_reward_for_rank(rank_position: int) -> int:
    """Возвращает плановую награду для места или 0 вне top-10."""
    return WEEKLY_REWARDS.get(rank_position, 0)


async def snapshot_leaderboard(
    session: AsyncSession,
    period_start: date,
    *,
    limit: int | None = None,
) -> list[ArenaLeaderboardSnapshot]:
    """Идемпотентно фиксирует глобальный Arena leaderboard за неделю.

    Snapshot immutable: если период уже был сохранён, повторный scheduler tick
    не переписывает рейтинг. ``limit=None`` сохраняет весь leaderboard; для
    еженедельных наград можно передать 10. Функция не коммитит.
    """
    normalized = week_start(period_start)
    if limit is not None and not 1 <= limit <= 10_000:
        raise ValueError("limit должен быть от 1 до 10000")

    profiles_result = await session.execute(
        select(ArenaProfile)
        .order_by(
            ArenaProfile.rating.desc(),
            ArenaProfile.wins.desc(),
            ArenaProfile.total_matches.desc(),
            ArenaProfile.user_id.asc(),
        )
        .limit(limit)
        if limit is not None
        else select(ArenaProfile).order_by(
            ArenaProfile.rating.desc(),
            ArenaProfile.wins.desc(),
            ArenaProfile.total_matches.desc(),
            ArenaProfile.user_id.asc(),
        )
    )
    profiles = list(profiles_result.scalars().all())
    if profiles:
        await session.execute(
            pg_insert(ArenaLeaderboardSnapshot)
            .values(
                [
                    {
                        "period_start": normalized,
                        "user_id": profile.user_id,
                        "rank_position": rank,
                        "rating": profile.rating,
                        "wins": profile.wins,
                        "matches": profile.total_matches,
                        "reward_amount": 0,
                    }
                    for rank, profile in enumerate(profiles, start=1)
                ]
            )
            .on_conflict_do_nothing(
                index_elements=["period_start", "user_id"]
            )
        )

    result = await session.execute(
        select(ArenaLeaderboardSnapshot)
        .where(ArenaLeaderboardSnapshot.period_start == normalized)
        .order_by(ArenaLeaderboardSnapshot.rank_position.asc())
    )
    return list(result.scalars().all())


async def _active_pvp_users(
    session: AsyncSession, chat_id: int, period_start: date
) -> set[int]:
    start, end = _period_bounds(period_start)
    result = await session.execute(
        select(ArenaMatch.creator_id, ArenaMatch.opponent_id)
        .where(
            ArenaMatch.chat_id == chat_id,
            ArenaMatch.status == "finished",
            ArenaMatch.resolved_at >= start,
            ArenaMatch.resolved_at < end,
        )
    )
    active: set[int] = set()
    for creator_id, opponent_id in result.all():
        active.add(creator_id)
        if opponent_id is not None:
            active.add(opponent_id)
    return active


async def nominate_weekly_awards(
    session: AsyncSession, chat_id: int, period_start: date
) -> list[ArenaWeeklyAward]:
    """Создаёт nomination rows для активных игроков из top-10 snapshot.

    Активность — хотя бы один завершённый PvP-матч в пределах snapshot-недели.
    Номинация не списывает и не начисляет ювики: ``reward_amount`` — только
    зафиксированный план. Глобальный ``fund_ledger_key`` не содержит chat_id:
    один глобальный рейтинг не может повторно номинировать того же игрока из
    нескольких чатов и тем самым подготовить двойную выплату.
    """
    normalized = week_start(period_start)
    active_users = await _active_pvp_users(session, chat_id, normalized)
    if not active_users:
        return []

    snapshot_result = await session.execute(
        select(ArenaLeaderboardSnapshot)
        .where(
            ArenaLeaderboardSnapshot.period_start == normalized,
            ArenaLeaderboardSnapshot.rank_position <= 10,
            ArenaLeaderboardSnapshot.user_id.in_(active_users),
        )
        .order_by(ArenaLeaderboardSnapshot.rank_position.asc())
    )
    snapshots = list(snapshot_result.scalars().all())
    if snapshots:
        await session.execute(
            pg_insert(ArenaWeeklyAward)
            .values(
                [
                    {
                        "chat_id": chat_id,
                        "week_start": normalized,
                        "award_key": f"rank_{snapshot.rank_position}",
                        "user_id": snapshot.user_id,
                        "reward_amount": weekly_reward_for_rank(snapshot.rank_position),
                        "fund_ledger_key": (
                            f"arena:weekly:{normalized}:"
                            f"rank_{snapshot.rank_position}:{snapshot.user_id}"
                        ),
                        "details": {
                            "rank": snapshot.rank_position,
                            "rating": snapshot.rating,
                            "wins": snapshot.wins,
                            "matches": snapshot.matches,
                        },
                    }
                    for snapshot in snapshots
                ]
            )
            # Без conflict target: кроме recipient-unique, нужно безопасно
            # поглощать и глобальный fund_ledger_key conflict при повторной
            # номинации этого игрока из другого чата.
            .on_conflict_do_nothing()
        )

    awards_result = await session.execute(
        select(ArenaWeeklyAward)
        .where(
            ArenaWeeklyAward.chat_id == chat_id,
            ArenaWeeklyAward.week_start == normalized,
        )
        .order_by(ArenaWeeklyAward.award_key.asc())
    )
    awards = list(awards_result.scalars().all())
    return sorted(awards, key=lambda award: (award.details or {}).get("rank", 0))


def register_weekly_snapshot(scheduler, *, hour: int = 23, minute: int = 50) -> None:
    """Регистрирует immutable snapshot + weekly settlement job перед границей
    недели (MSK). ``replace_existing`` и ``max_instances=1`` защищают от
    дублей при повторной инициализации scheduler.
    """
    from bot.config import settings
    from bot.services import scheduler as scheduler_service
    from common.db.session import SessionLocal

    async def _job() -> None:
        async with SessionLocal() as session:
            try:
                today = datetime.now(scheduler_service.MSK).date()
                normalized = week_start(today)
                # Сохраняем полный immutable срез. Ограничение top-10 применяется
                # только при nomination, иначе игроки ниже десятого места
                # навсегда теряют историю рейтинга.
                await snapshot_leaderboard(session, normalized)
                await nominate_weekly_awards(session, settings.chat_id, normalized)
                await _settle_weekly_awards(session, settings.chat_id, normalized)
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    scheduler.add_job(
        _job,
        "cron",
        day_of_week="sun",
        hour=hour,
        minute=minute,
        timezone=scheduler_service.MSK,
        coalesce=True,
        max_instances=1,
        misfire_grace_time=3600,
        id=_WEEKLY_SNAPSHOT_JOB_ID,
        replace_existing=True,
    )
