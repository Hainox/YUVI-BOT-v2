from __future__ import annotations

from bot.services import arena_runtime_worker


class _Scheduler:
    def __init__(self):
        self.jobs = []

    def add_job(self, func, trigger, **kwargs):
        self.jobs.append((func, trigger, kwargs))


def test_register_runtime_tick_job():
    scheduler = _Scheduler()
    arena_runtime_worker.register_runtime_tick(scheduler)

    assert len(scheduler.jobs) == 1
    _, trigger, kwargs = scheduler.jobs[0]
    assert trigger == "interval"
    assert kwargs["seconds"] == 1
    assert kwargs["id"] == "arena_runtime_tick"
