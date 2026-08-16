from __future__ import annotations

from datetime import timedelta
from datetime import timezone
from types import SimpleNamespace
from unittest.mock import Mock

from bot.services.arena_daily_awards_service import _event_stats
from bot.services import arena_daily_awards_service


MSK = timezone(timedelta(hours=3), "MSK")


def _match(*, match_id: int = 1, creator_id: int = 10, opponent_id: int = 20, phase_count: int = 3):
    return SimpleNamespace(
        id=match_id,
        creator_id=creator_id,
        opponent_id=opponent_id,
        phase_count=phase_count,
    )


def _event(phase_index: int, **payload):
    return SimpleNamespace(phase_index=phase_index, payload=payload)


def test_counter_nomination_metric_uses_player_with_most_counters() -> None:
    stats = _event_stats(
        [
            _event(0, player_counter=True, player_damage=8),
            _event(1, opponent_counter=True, opponent_damage=12),
            _event(2, opponent_counter=True, opponent_damage=10),
        ],
        _match(),
    )

    assert stats["counter_count"] == 3
    assert stats["counter_winner_id"] == 20
    assert stats["counter_damage"] == 12


def test_counter_tie_breaks_by_first_phase_then_user_id() -> None:
    stats = _event_stats(
        [
            _event(2, player_counter=True, player_damage=9),
            _event(0, opponent_counter=True, opponent_damage=7),
        ],
        _match(),
    )

    assert stats["counter_winner_id"] == 20


def test_daily_nomination_runs_before_existing_digest_schedule() -> None:
    scheduler = Mock()

    arena_daily_awards_service.register_daily_nomination(scheduler)

    job = scheduler.add_job.call_args
    assert job.args[1] == "cron"
    assert job.kwargs["hour"] == 21
    assert job.kwargs["minute"] == 50
    assert job.kwargs["id"] == "arena_daily_award_nomination"
    assert job.kwargs["replace_existing"] is True
    assert job.kwargs["max_instances"] == 1
