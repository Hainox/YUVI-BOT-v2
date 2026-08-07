"""Periodic server clock for active Arena sessions."""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)
_RUNTIME_JOB_ID = "arena_runtime_tick"
_runtime_client = None


async def shutdown_runtime_tick() -> None:
    """Close the worker-owned Redis client during bot shutdown."""
    global _runtime_client
    if _runtime_client is not None:
        client, _runtime_client = _runtime_client, None
        await client.aclose()


def register_runtime_tick(scheduler, *, interval_seconds: int = 1) -> None:
    async def _job() -> None:
        global _runtime_client
        from bot.config import settings

        if not settings.redis_url:
            return

        import redis.asyncio as redis
        from sqlalchemy import select

        from bot.services.arena_session_service import ArenaSessionService
        from bot.services.arena_session_service import persist_runtime_state
        from common.db.session import SessionLocal
        from common.models.arena import ArenaMatch

        if _runtime_client is None:
            _runtime_client = redis.from_url(settings.redis_url)

        try:
            async with SessionLocal() as session:
                result = await session.execute(
                    select(ArenaMatch).where(ArenaMatch.status == "active").limit(500)
                )
                matches = list(result.scalars().all())
            service = ArenaSessionService(_runtime_client, persist_state=persist_runtime_state)
            for match in matches:
                try:
                    await service.tick(match)
                except Exception:
                    logger.exception("Arena runtime tick failed match_id=%s; resetting Redis client", match.id)
                    await shutdown_runtime_tick()
                    break
        except Exception:
            logger.exception("Arena runtime worker tick failed; resetting Redis client")
            await shutdown_runtime_tick()

    scheduler.add_job(
        _job,
        "interval",
        seconds=interval_seconds,
        coalesce=True,
        max_instances=1,
        misfire_grace_time=5,
        id=_RUNTIME_JOB_ID,
        replace_existing=True,
    )
