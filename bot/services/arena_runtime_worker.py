"""Periodic server clock for active Arena sessions.

Also the Phase-5 settlement trigger: this loop already re-scans every
`ArenaMatch.status == "active"` row every second and ticks its combat
session, so it's the cheapest, fastest place to notice a match going
terminal (knockout/time-limit/technical loss/both-disconnected — any of
`ArenaSessionService`'s four termination paths) and hand it to
`arena_service.settle_match` — no second table scan needed. See
`arena_service.py`'s "Settlement" section docstring for why this hook
exists at all."""

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

        from bot.services import arena_service
        from bot.services import balance_events
        from bot.services import economy_service
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
                    state = await service.tick(match)
                except Exception:
                    logger.exception("Arena runtime tick failed match_id=%s; resetting Redis client", match.id)
                    await shutdown_runtime_tick()
                    break
                if not state["terminal"]:
                    continue
                async with SessionLocal() as settle_session:
                    try:
                        settled = await arena_service.settle_match(
                            settle_session, match.chat_id, match.id, state
                        )
                    except Exception:
                        logger.exception("Arena settlement failed match_id=%s", match.id)
                        continue
                    if settled.settlement_status == "pending":
                        continue
                    for user_id in (settled.creator_id, settled.opponent_id):
                        if user_id is None:
                            continue
                        try:
                            balance = await economy_service.get_balance(
                                settle_session, settled.chat_id, user_id
                            )
                            await balance_events.publish_balance(
                                _runtime_client, settled.chat_id, user_id, balance
                            )
                        except Exception:
                            logger.debug(
                                "Arena settlement balance publish failed match_id=%s user_id=%s",
                                match.id,
                                user_id,
                                exc_info=True,
                            )
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
