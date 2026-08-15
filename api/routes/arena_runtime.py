"""Arena runtime API.

The lobby router owns money and lifecycle before the fight. This module owns
authenticated access to the Redis-backed combat session and invokes the
idempotent settlement consumer whenever a terminal snapshot is observed;
the scheduler remains the recovery path for interrupted requests.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from pydantic import Field
from sqlalchemy import select

from api.deps import AuthContext
from api.deps import require_membership
from api.arena_rate_limit import enforce_arena_rate_limit
from bot.services import arena_service
from bot.services import arena_session_service
from bot.services import balance_events
from bot.services import economy_service
from common.db.session import SessionLocal
from common.models.arena import ArenaMatch

router = APIRouter()
logger = logging.getLogger(__name__)


class ArenaActionBody(BaseModel):
    action: str
    client_action_id: str = Field(min_length=1, max_length=120)


async def _load_match(chat_id: int, match_id: int, user_id: int) -> ArenaMatch:
    async with SessionLocal() as session:
        match = (
            await session.execute(
                select(ArenaMatch).where(
                    ArenaMatch.chat_id == chat_id,
                    ArenaMatch.id == match_id,
                )
            )
        ).scalar_one_or_none()
    if match is None:
        raise HTTPException(status_code=404, detail="Матч не найден")
    if match.status != "active":
        raise HTTPException(status_code=409, detail="Матч не активен")
    if user_id not in {match.creator_id, match.opponent_id}:
        raise HTTPException(status_code=403, detail="Вы не участник этого матча")
    return match


def _runtime(request: Request) -> arena_session_service.ArenaSessionService:
    redis_client = getattr(request.app.state, "redis", None)
    if redis_client is None:
        raise HTTPException(status_code=503, detail="Arena runtime недоступен")
    return arena_session_service.ArenaSessionService(
        redis_client,
        persist_state=arena_session_service.persist_runtime_state,
    )


async def _settle_terminal_state(request: Request, match: ArenaMatch, state: dict) -> None:
    """Close a terminal runtime snapshot before returning it to the client.

    The scheduler remains the recovery/retry path, but doing the same idempotent
    settlement in the request that observed terminal state removes the result
    page's transient 409 window for normal knockout/forfeit flows.
    """
    if not state.get("terminal"):
        return
    async with SessionLocal() as session:
        try:
            settled = await arena_service.settle_match(
                session, match.chat_id, match.id, state
            )
        except Exception:  # noqa: BLE001 - scheduler will retry the active row
            logger.exception("Arena request settlement failed match_id=%s", match.id)
            return
        for user_id in (settled.creator_id, settled.opponent_id):
            if user_id is None:
                continue
            try:
                balance = await economy_service.get_balance(
                    session, settled.chat_id, user_id
                )
                await balance_events.publish_balance(
                    request.app.state.redis, settled.chat_id, user_id, balance
                )
            except Exception:  # noqa: BLE001 - live balance is best-effort
                logger.debug(
                    "Arena request balance publish failed match_id=%s user_id=%s",
                    match.id,
                    user_id,
                    exc_info=True,
                )


def _map_session_error(exc: arena_session_service.ArenaSessionError) -> HTTPException:
    if isinstance(exc, arena_session_service.SessionNotFound):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, arena_session_service.UnauthorizedSession):
        return HTTPException(status_code=403, detail=str(exc))
    if isinstance(exc, arena_session_service.InvalidAction):
        return HTTPException(status_code=422, detail=str(exc))
    if isinstance(exc, arena_session_service.DuplicateAction):
        return HTTPException(status_code=409, detail=str(exc))
    return HTTPException(status_code=409, detail=str(exc))


@router.get("/api/v1/arena/matches/{match_id}")
async def get_arena_runtime_state(
    match_id: int,
    request: Request,
    auth: AuthContext = Depends(require_membership),
) -> dict:
    match = await _load_match(auth.chat_id, match_id, auth.user_id)
    runtime = _runtime(request)
    try:
        if match.status == "active":
            state = await runtime.start_or_get(match)
            await _settle_terminal_state(request, match, state)
            return runtime.public_state(state, auth.user_id)
        return await runtime.get_state(match, viewer_id=auth.user_id)
    except arena_session_service.ArenaSessionError as exc:
        raise _map_session_error(exc) from exc


@router.post("/api/v1/arena/matches/{match_id}/actions")
async def post_arena_action(
    match_id: int,
    body: ArenaActionBody,
    request: Request,
    auth: AuthContext = Depends(require_membership),
) -> dict:
    await enforce_arena_rate_limit(
        request,
        auth,
        operation="submit_action",
        limit=20,
        window_seconds=10,
    )
    match = await _load_match(auth.chat_id, match_id, auth.user_id)
    runtime = _runtime(request)
    try:
        state = await runtime.submit_action(
            match,
            auth.user_id,
            body.action,
            body.client_action_id,
        )
        await _settle_terminal_state(request, match, state)
        return state
    except arena_session_service.ArenaSessionError as exc:
        raise _map_session_error(exc) from exc


@router.post("/api/v1/arena/matches/{match_id}/reconnect")
async def post_arena_reconnect(
    match_id: int,
    request: Request,
    auth: AuthContext = Depends(require_membership),
) -> dict:
    await enforce_arena_rate_limit(
        request,
        auth,
        operation="reconnect",
        limit=6,
        window_seconds=30,
    )
    match = await _load_match(auth.chat_id, match_id, auth.user_id)
    runtime = _runtime(request)
    try:
        state = await runtime.reconnect(match, auth.user_id)
        await _settle_terminal_state(request, match, state)
        return state
    except arena_session_service.ArenaSessionError as exc:
        raise _map_session_error(exc) from exc


@router.post("/api/v1/arena/matches/{match_id}/disconnect")
async def post_arena_disconnect(
    match_id: int,
    request: Request,
    auth: AuthContext = Depends(require_membership),
) -> dict:
    match = await _load_match(auth.chat_id, match_id, auth.user_id)
    runtime = _runtime(request)
    try:
        state = await runtime.disconnect(match, auth.user_id)
        await _settle_terminal_state(request, match, state)
        return state
    except arena_session_service.ArenaSessionError as exc:
        raise _map_session_error(exc) from exc


@router.post("/api/v1/arena/matches/{match_id}/forfeit")
async def post_arena_forfeit(
    match_id: int,
    request: Request,
    auth: AuthContext = Depends(require_membership),
) -> dict:
    match = await _load_match(auth.chat_id, match_id, auth.user_id)
    runtime = _runtime(request)
    try:
        state = await runtime.forfeit(match, auth.user_id)
        await _settle_terminal_state(request, match, state)
        return state
    except arena_session_service.ArenaSessionError as exc:
        raise _map_session_error(exc) from exc


async def _arena_event_stream(
    redis_client,
    channel: str,
    is_disconnected,
) -> AsyncIterator[str]:
    """Stream only the authenticated match channel, with proxy heartbeats."""
    try:
        async with redis_client.pubsub() as pubsub:
            await pubsub.subscribe(channel)
            while True:
                if await is_disconnected():
                    return
                message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=20.0)
                if message is None:
                    yield ": ping\n\n"
                    continue
                raw = message.get("data")
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8")
                if not raw:
                    continue
                yield f"data: {json.dumps(json.loads(raw), ensure_ascii=False)}\n\n"
    except Exception:
        return


@router.get("/api/v1/arena/matches/{match_id}/events")
async def get_arena_events(
    match_id: int,
    request: Request,
    auth: AuthContext = Depends(require_membership),
) -> StreamingResponse:
    match = await _load_match(auth.chat_id, match_id, auth.user_id)
    redis_client = getattr(request.app.state, "redis", None)
    if redis_client is None:
        raise HTTPException(status_code=503, detail="Arena runtime недоступен")
    return StreamingResponse(
        _arena_event_stream(
            redis_client,
            arena_session_service.ArenaSessionService.event_channel(match.id, match.chat_id),
            request.is_disconnected,
        ),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
