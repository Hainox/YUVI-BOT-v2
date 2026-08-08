"""Small Redis-backed abuse guard for state-changing Arena requests.

Arena's combat endpoints are already Redis-dependent, but the limiter is
fail-open: a temporary Redis outage must not turn a money-safe API into a
blanket 503. The key contains only verified AuthContext identifiers and a
logical operation name; Telegram initData is never stored in Redis.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from fastapi import HTTPException
from fastapi import Request

from api.deps import AuthContext

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RateLimitDecision:
    allowed: bool
    retry_after: int = 0


def rate_limit_key(auth: AuthContext, operation: str) -> str:
    """Build a tenant- and verified-user-scoped Redis key."""
    safe_operation = operation.replace(":", "_")
    return f"arena:ratelimit:{auth.chat_id}:{auth.user_id}:{safe_operation}"


async def check_arena_rate_limit(
    redis_client,
    *,
    key: str,
    limit: int,
    window_seconds: int,
) -> RateLimitDecision:
    """Apply a fixed-window counter, returning fail-open on Redis errors.

    ``INCR`` is atomic in Redis. The expiry is set only on the first request,
    so a burst cannot extend its own block indefinitely. If the expiry command
    itself fails, the request is allowed and the error is logged rather than
    risking a counter without a TTL blocking a user forever.
    """
    if redis_client is None:
        return RateLimitDecision(allowed=True)
    if limit < 1 or window_seconds < 1:
        raise ValueError("rate-limit limit and window must be positive")

    try:
        count = int(await redis_client.incr(key))
        if count == 1:
            try:
                await redis_client.expire(key, window_seconds)
            except Exception:  # noqa: BLE001 - never create a permanent lockout
                logger.warning("Arena rate-limit expiry failed key=%s", key, exc_info=True)
                return RateLimitDecision(allowed=True)
        if count <= limit:
            return RateLimitDecision(allowed=True)

        retry_after = window_seconds
        try:
            ttl = int(await redis_client.ttl(key))
            if ttl > 0:
                retry_after = ttl
        except Exception:  # noqa: BLE001 - fallback to the configured window
            logger.debug("Arena rate-limit TTL lookup failed key=%s", key, exc_info=True)
        return RateLimitDecision(allowed=False, retry_after=max(1, retry_after))
    except Exception:  # noqa: BLE001 - rate limiting is deliberately fail-open
        logger.warning("Arena rate-limit check failed key=%s", key, exc_info=True)
        return RateLimitDecision(allowed=True)


async def enforce_arena_rate_limit(
    request: Request,
    auth: AuthContext,
    *,
    operation: str,
    limit: int,
    window_seconds: int,
) -> None:
    """Raise a stable 429 response when the verified actor exceeds a limit."""
    redis_client = getattr(request.app.state, "redis", None)
    decision = await check_arena_rate_limit(
        redis_client,
        key=rate_limit_key(auth, operation),
        limit=limit,
        window_seconds=window_seconds,
    )
    if not decision.allowed:
        raise HTTPException(
            status_code=429,
            detail="Слишком много запросов к Arena. Попробуйте чуть позже.",
            headers={"Retry-After": str(decision.retry_after)},
        )
