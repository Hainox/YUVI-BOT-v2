from __future__ import annotations

from datetime import date
from datetime import datetime
from datetime import timedelta
from unittest.mock import Mock
from datetime import timezone

from bot.services import arena_awards_service
from common.models.arena import ArenaWeeklyAward


MSK = timezone(timedelta(hours=3), "MSK")


def test_week_start_normalizes_any_day_to_monday() -> None:
    assert arena_awards_service.week_start(date(2026, 8, 9)) == date(2026, 8, 3)
    assert arena_awards_service.week_start(date(2026, 8, 3)) == date(2026, 8, 3)


def test_period_bounds_use_moscow_week_boundary() -> None:
    start, end = arena_awards_service._period_bounds(date(2026, 8, 9))

    assert start == datetime(2026, 8, 3, tzinfo=MSK)
    assert end - start == timedelta(days=7)
    assert start.tzinfo == MSK
    assert end.astimezone(timezone.utc).date() == date(2026, 8, 9)


def test_weekly_reward_schedule_is_top_ten_only() -> None:
    assert arena_awards_service.weekly_reward_for_rank(1) == 25_000
    assert arena_awards_service.weekly_reward_for_rank(10) == 5_000
    assert arena_awards_service.weekly_reward_for_rank(11) == 0


def test_weekly_award_has_global_idempotency_key_constraint() -> None:
    names = {
        constraint.name
        for constraint in ArenaWeeklyAward.__table__.constraints
        if constraint.name
    }
    assert "uq_arena_weekly_awards_fund_key" in names


def test_register_weekly_snapshot_is_replaceable_and_full_snapshot() -> None:
    scheduler = Mock()

    arena_awards_service.register_weekly_snapshot(scheduler, hour=23, minute=50)

    scheduler.add_job.assert_called_once()
    job = scheduler.add_job.call_args
    assert job.args[1] == "cron"
    assert job.kwargs["id"] == "arena_weekly_leaderboard_snapshot"
    assert job.kwargs["replace_existing"] is True
    assert job.kwargs["max_instances"] == 1
    assert job.kwargs["day_of_week"] == "sun"
    assert job.kwargs["hour"] == 23
    assert job.kwargs["minute"] == 50
