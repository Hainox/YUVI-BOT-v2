from __future__ import annotations

from unittest.mock import Mock

from bot.services import arena_service


def test_register_expiry_job_is_idempotent_and_short_interval() -> None:
    scheduler = Mock()

    arena_service.register_expiry_job(scheduler, interval_seconds=15)
    arena_service.register_expiry_job(scheduler, interval_seconds=15)

    assert scheduler.add_job.call_count == 2
    for call in scheduler.add_job.call_args_list:
        assert call.args[1] == "interval"
        assert call.kwargs["seconds"] == 15
        assert call.kwargs["id"] == "arena_match_expiry"
        assert call.kwargs["replace_existing"] is True
        assert call.kwargs["max_instances"] == 1
