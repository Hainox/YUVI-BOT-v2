"""POST /api/v1/duel (create) + /duel/{id}/accept + /duel/{id}/decline +
/duel/{id}/cancel + POST /api/v1/duelbot — thin routes over
`bot.services.duel_service` (DUEL-01/DUEL-02, D-04). `user_id`/`chat_id`
come ONLY from `AuthContext` (`require_membership`) — challenger/opponent/
actor identity is NEVER taken from the request body (IDOR, T-04.2-01/02):
`CreateDuelBody.opponent_id` names WHO is being challenged (a legitimate
body field, the target), never WHO is acting.

Mute-from-api (Pitfall 7, T-04.2-11): the `api` process has no aiogram
`Bot`, so after `duel_service.accept_duel`/`duelbot` resolves a duel
(returns `loser_id`/`mute_seconds`), this route calls
`api.duel_mute.apply_mute_from_api` directly — a raw httpx POST to
Telegram's `restrictChatMember` — reusing `request.app.state.http_client`
(singleton, never create a new `httpx.AsyncClient` per request) and the
shared `bot.services.duel_constants.MUTE_PERMISSIONS` dict (no duplicated
permission literals vs. `bot/handlers/duel.py`). The call is wrapped in its
own try/except at this layer (belt-and-braces on top of
`apply_mute_from_api`'s own fail-closed swallow) — money already moved via
`duel_service`, so a mute-path exception must never turn an
already-successful accept/duelbot response into an error for the client
(same WR-05 precedent as `bot/handlers/duel.py::duel_accept_command`/
`duelbot_command`).

Exception mapping: `DuelNotFound` -> 404 (checked BEFORE the broader
`DuelError` except, since it's a subclass); `DuelError` (including
`DuelAlreadyResolved` — replay with the same `ref_id`) -> 400;
`economy_service.InsufficientFunds` -> 400.

After every money-moving call this route re-reads and publishes the
affected balance(s) to `bal:{chat_id}` via `balance_events.publish_balance`
(D-02) — the same pattern already established by `api/routes/games.py`/
`farm.py`/`gacha.py` for this phase; omitting it here would repeat the
exact "SSE channel wired but never fed" gap already found and fixed in
04.2-02 for a different route.
"""

from __future__ import annotations

import logging
from datetime import datetime
from datetime import timedelta

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Request
from pydantic import BaseModel
from pydantic import Field

from api.deps import AuthContext
from api.deps import require_membership
from api.duel_mute import apply_mute_from_api
from bot.config import settings
from bot.services import balance_events
from bot.services import duel_constants
from bot.services import duel_service
from bot.services import economy_service
from common.db.session import SessionLocal

logger = logging.getLogger(__name__)

router = APIRouter()


class CreateDuelBody(BaseModel):
    opponent_id: int
    stake: int = Field(ge=1)
    ref_id: str


class AcceptDuelBody(BaseModel):
    ref_id: str


class DuelbotBody(BaseModel):
    stake: int = Field(ge=1)
    ref_id: str


async def _publish_balance(session, redis_client, chat_id: int, user_id: int) -> None:
    balance = await economy_service.get_balance(session, chat_id, user_id)
    await balance_events.publish_balance(redis_client, chat_id, user_id, balance)


async def _mute_loser(request: Request, chat_id: int, result: dict) -> None:
    loser_id = result.get("loser_id")
    mute_seconds = result.get("mute_seconds")
    if loser_id is None or not mute_seconds:
        return
    until = datetime.utcnow() + timedelta(seconds=mute_seconds)
    try:
        await apply_mute_from_api(
            request.app.state.http_client,
            settings.bot_token,
            chat_id,
            loser_id,
            until,
            duel_constants.MUTE_PERMISSIONS,
        )
    except Exception:
        # Money already moved (accept_duel/duelbot committed) — a mute-path
        # failure must never break the response to the client.
        logger.exception("duel mute-from-api failed chat_id=%s loser_id=%s", chat_id, loser_id)


@router.post("/api/v1/duel")
async def post_create_duel(
    body: CreateDuelBody, request: Request, auth: AuthContext = Depends(require_membership)
) -> dict:
    async with SessionLocal() as session:
        try:
            duel = await duel_service.create_duel(
                session, auth.chat_id, auth.user_id, body.opponent_id, body.stake, body.ref_id
            )
        except duel_service.DuelError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except economy_service.InsufficientFunds as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        await _publish_balance(session, request.app.state.redis, auth.chat_id, auth.user_id)

        return {
            "duel_id": duel.id,
            "status": duel.status,
            "challenger_id": duel.challenger_id,
            "opponent_id": duel.opponent_id,
            "stake": duel.stake,
        }


@router.post("/api/v1/duel/{duel_id}/accept")
async def post_accept_duel(
    duel_id: int,
    body: AcceptDuelBody,
    request: Request,
    auth: AuthContext = Depends(require_membership),
) -> dict:
    async with SessionLocal() as session:
        try:
            result = await duel_service.accept_duel(
                session, auth.chat_id, duel_id, auth.user_id, body.ref_id
            )
        except duel_service.DuelNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except duel_service.DuelError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except economy_service.InsufficientFunds as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        if result.get("status") == "resolved":
            winner_id = result.get("winner_id")
            loser_id = result.get("loser_id")
            if winner_id is not None:
                await _publish_balance(session, request.app.state.redis, auth.chat_id, winner_id)
            if loser_id is not None:
                await _publish_balance(session, request.app.state.redis, auth.chat_id, loser_id)
            await _mute_loser(request, auth.chat_id, result)

        return result


@router.post("/api/v1/duel/{duel_id}/decline")
async def post_decline_duel(
    duel_id: int, request: Request, auth: AuthContext = Depends(require_membership)
) -> dict:
    async with SessionLocal() as session:
        try:
            result = await duel_service.decline_duel(session, auth.chat_id, duel_id, auth.user_id)
        except duel_service.DuelNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except duel_service.DuelError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        if result.get("status") == "declined":
            challenger_id = (await session.get(duel_service.Duel, duel_id)).challenger_id
            await _publish_balance(session, request.app.state.redis, auth.chat_id, challenger_id)

        return result


@router.post("/api/v1/duel/{duel_id}/cancel")
async def post_cancel_duel(
    duel_id: int, request: Request, auth: AuthContext = Depends(require_membership)
) -> dict:
    async with SessionLocal() as session:
        try:
            result = await duel_service.cancel_duel(session, auth.chat_id, duel_id, auth.user_id)
        except duel_service.DuelNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except duel_service.DuelError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        if result.get("status") == "cancelled":
            await _publish_balance(session, request.app.state.redis, auth.chat_id, auth.user_id)

        return result


@router.post("/api/v1/duelbot")
async def post_duelbot(
    body: DuelbotBody, request: Request, auth: AuthContext = Depends(require_membership)
) -> dict:
    async with SessionLocal() as session:
        try:
            result = await duel_service.duelbot(
                session, auth.chat_id, auth.user_id, body.stake, body.ref_id
            )
        except duel_service.DuelError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except economy_service.InsufficientFunds as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        await _publish_balance(session, request.app.state.redis, auth.chat_id, auth.user_id)
        await _mute_loser(request, auth.chat_id, result)

        return result
