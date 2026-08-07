"""Arena lobby API (Phase 3).

All actor identity comes from the verified AuthContext. Money moves only through
arena_service -> economy_service; this router never writes balances directly.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Request
from pydantic import BaseModel
from pydantic import Field

from api.deps import AuthContext
from api.deps import require_membership
from bot.services import arena_service
from bot.services import balance_events
from bot.services import economy_service
from common.arena.schemas import FighterType
from common.db.session import SessionLocal
from common.models.arena import ArenaMatch

router = APIRouter()


class CreateArenaMatchBody(BaseModel):
    fighter: FighterType
    bet: int = Field(ge=100)
    idempotency_key: str = Field(min_length=1, max_length=120)


class AcceptArenaMatchBody(BaseModel):
    fighter: FighterType
    bet: int = Field(ge=100)
    idempotency_key: str = Field(min_length=1, max_length=120)


class ArenaMatchResponse(BaseModel):
    id: int
    status: str
    creator_id: int
    creator_bet: int
    opponent_id: int | None = None
    opponent_bet: int | None = None
    expires_at: datetime
    accept_deadline: datetime | None = None
    creator_fighter: FighterType | None = None
    opponent_fighter: FighterType | None = None
    creator_confirmed: bool = False
    opponent_confirmed: bool = False
    started_at: datetime | None = None
    user_balance_after: int | None = None


def _serialize_match(
    match: ArenaMatch,
    *,
    viewer_id: int | None = None,
    include_fighters: bool = False,
) -> dict:
    """Expose only lobby-safe fields; fighters stay hidden until the viewer owns them."""
    payload = {
        "id": match.id,
        "status": match.status,
        "creator_id": match.creator_id,
        "creator_bet": match.creator_bet,
        "opponent_id": match.opponent_id,
        "opponent_bet": match.opponent_bet,
        "expires_at": match.expires_at,
        "accept_deadline": match.accept_deadline,
        "creator_confirmed": match.creator_confirmed,
        "opponent_confirmed": match.opponent_confirmed,
        "started_at": match.started_at,
    }
    if include_fighters or match.status == "active":
        payload["creator_fighter"] = match.creator_fighter
        payload["opponent_fighter"] = match.opponent_fighter
    elif viewer_id == match.creator_id:
        payload["creator_fighter"] = match.creator_fighter
    elif viewer_id == match.opponent_id:
        payload["opponent_fighter"] = match.opponent_fighter
    return payload


async def _publish_balance(request: Request, chat_id: int, user_id: int) -> int:
    async with SessionLocal() as session:
        balance = await economy_service.get_balance(session, chat_id, user_id)
    await balance_events.publish_balance(request.app.state.redis, chat_id, user_id, balance)
    return balance


@router.get("/api/v1/arena/matches")
async def get_arena_matches(
    auth: AuthContext = Depends(require_membership),
) -> list[dict]:
    async with SessionLocal() as session:
        matches = await arena_service.list_open_matches(session, auth.chat_id)
    return [_serialize_match(match) for match in matches]


@router.post("/api/v1/arena/matches", response_model=ArenaMatchResponse, status_code=201)
async def post_create_arena_match(
    body: CreateArenaMatchBody,
    request: Request,
    auth: AuthContext = Depends(require_membership),
) -> dict:
    async with SessionLocal() as session:
        try:
            match = await arena_service.create_match(
                session,
                auth.chat_id,
                auth.user_id,
                body.fighter,
                body.bet,
                body.idempotency_key,
            )
        except arena_service.InvalidMatch as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except arena_service.DuplicateRequest as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except economy_service.InsufficientFunds as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    balance = await _publish_balance(request, auth.chat_id, auth.user_id)
    payload = _serialize_match(match, viewer_id=auth.user_id, include_fighters=True)
    payload["user_balance_after"] = balance
    return payload


@router.post(
    "/api/v1/arena/matches/{match_id}/accept",
    response_model=ArenaMatchResponse,
)
async def post_accept_arena_match(
    match_id: int,
    body: AcceptArenaMatchBody,
    request: Request,
    auth: AuthContext = Depends(require_membership),
) -> dict:
    async with SessionLocal() as session:
        try:
            match = await arena_service.accept_match(
                session,
                auth.chat_id,
                match_id,
                auth.user_id,
                body.fighter,
                body.bet,
                body.idempotency_key,
            )
        except arena_service.MatchNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except arena_service.MatchConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except arena_service.InvalidMatch as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except arena_service.DuplicateRequest as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except economy_service.InsufficientFunds as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    balance = await _publish_balance(request, auth.chat_id, auth.user_id)
    payload = _serialize_match(match, viewer_id=auth.user_id)
    payload["user_balance_after"] = balance
    return payload


@router.post("/api/v1/arena/matches/{match_id}/confirm", response_model=ArenaMatchResponse)
async def post_confirm_arena_match(
    match_id: int,
    request: Request,
    auth: AuthContext = Depends(require_membership),
) -> dict:
    async with SessionLocal() as session:
        try:
            match = await arena_service.confirm_match(
                session, auth.chat_id, match_id, auth.user_id, now=datetime.utcnow()
            )
        except arena_service.MatchNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except arena_service.MatchConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    return _serialize_match(match, viewer_id=auth.user_id)


@router.post("/api/v1/arena/matches/{match_id}/cancel", response_model=ArenaMatchResponse)
async def post_cancel_arena_match(
    match_id: int,
    request: Request,
    auth: AuthContext = Depends(require_membership),
) -> dict:
    async with SessionLocal() as session:
        try:
            match = await arena_service.cancel_match(
                session, auth.chat_id, match_id, auth.user_id
            )
        except arena_service.MatchNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except arena_service.MatchConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    balance = await _publish_balance(request, auth.chat_id, auth.user_id)
    payload = _serialize_match(match, viewer_id=auth.user_id, include_fighters=True)
    payload["user_balance_after"] = balance
    return payload
