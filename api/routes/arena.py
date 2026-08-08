"""Arena lobby and read-only result/profile API.

All actor identity comes from the verified AuthContext. Money moves only through
arena_service -> economy_service; this router never writes balances directly.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Query
from fastapi import Request
from pydantic import BaseModel
from pydantic import Field
from sqlalchemy import select
from sqlalchemy.orm import aliased

from api.deps import AuthContext
from api.deps import require_membership
from api.arena_rate_limit import enforce_arena_rate_limit
from bot.services import arena_service
from bot.services import balance_events
from bot.services import economy_service
from common.arena.schemas import FighterType
from common.db.session import SessionLocal
from common.models.arena import ArenaFighterProgress
from common.models.arena import ArenaMatch
from common.models.arena import ArenaProfile
from common.models.user import User

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


def _display_name(user: User | None, user_id: int | None = None) -> str | None:
    if user is None:
        return str(user_id) if user_id is not None else None
    return user.username or user.first_name or str(user.id)


def _viewer_match(
    match: ArenaMatch, viewer_id: int
) -> tuple[int, int, str, str | None, int, int]:
    """Return viewer/opponent fields for a completed match."""
    if viewer_id == match.creator_id:
        return (
            match.creator_id,
            match.opponent_id or 0,
            match.creator_fighter,
            match.opponent_fighter,
            match.creator_bet,
            match.opponent_bet or 0,
        )
    if viewer_id == match.opponent_id and match.opponent_id is not None:
        return (
            match.opponent_id,
            match.creator_id,
            match.opponent_fighter or "",
            match.creator_fighter,
            match.opponent_bet or 0,
            match.creator_bet,
        )
    raise HTTPException(status_code=403, detail="Вы не участник этого матча")


def _serialize_arena_match_result(
    match: ArenaMatch,
    *,
    viewer_id: int,
    viewer: User | None = None,
    opponent: User | None = None,
    rating: int | None = None,
) -> dict:
    viewer_user_id, opponent_user_id, viewer_fighter, opponent_fighter, viewer_bet, opponent_bet = _viewer_match(
        match, viewer_id
    )
    won = match.winner_id == viewer_id
    lost = match.loser_id == viewer_id
    is_draw = match.match_result == "draw"
    technical_loss = match.match_result == "technical_loss" and lost
    rating_delta = 0 if is_draw else 25 if won else -30 if technical_loss else -20 if lost else 0
    xp_gained = 0 if technical_loss else 150 if won else 100 if is_draw or lost else 0
    user_refund = viewer_bet if match.settlement_status == "refunded" else 0
    user_payout = match.payout_amount if won and match.settlement_status == "paid" else 0
    return {
        "id": match.id,
        "status": match.status,
        "result": match.match_result,
        "reason": match.result_reason,
        "settlement_status": match.settlement_status,
        "viewer_id": viewer_user_id,
        "opponent_id": opponent_user_id,
        "viewer_name": _display_name(viewer, viewer_user_id),
        "opponent_name": _display_name(opponent, opponent_user_id),
        "viewer_fighter": viewer_fighter,
        "opponent_fighter": opponent_fighter,
        "viewer_bet": viewer_bet,
        "opponent_bet": opponent_bet,
        "payout_amount": user_payout,
        "refund_amount": user_refund,
        "rating_delta": rating_delta,
        "rating": rating,
        "xp_gained": xp_gained,
        "started_at": match.started_at,
        "finished_at": match.finished_at,
    }


@router.get("/api/v1/arena/profile")
async def get_arena_profile(auth: AuthContext = Depends(require_membership)) -> dict:
    """Return the authenticated user's all-time Arena rating and fighter XP."""
    async with SessionLocal() as session:
        profile = (
            await session.execute(
                select(ArenaProfile).where(ArenaProfile.user_id == auth.user_id)
            )
        ).scalar_one_or_none()
        progress = []
        if profile is not None:
            rows = (
                await session.execute(
                    select(ArenaFighterProgress)
                    .where(ArenaFighterProgress.profile_id == profile.id)
                    .order_by(ArenaFighterProgress.fighter_type)
                )
            ).scalars().all()
            progress = [
                {"fighter": row.fighter_type, "xp": row.xp, "level": row.level, "cosmetics": row.cosmetics}
                for row in rows
            ]
    return {
        "user_id": auth.user_id,
        "rating": profile.rating if profile is not None else 1000,
        "total_matches": profile.total_matches if profile is not None else 0,
        "wins": profile.wins if profile is not None else 0,
        "losses": profile.losses if profile is not None else 0,
        "draws": profile.draws if profile is not None else 0,
        "technical_wins": profile.technical_wins if profile is not None else 0,
        "technical_losses": profile.technical_losses if profile is not None else 0,
        "knockouts": profile.knockouts if profile is not None else 0,
        "fighters": progress,
    }


@router.get("/api/v1/arena/history")
async def get_arena_history(
    limit: int = Query(30, ge=1, le=100),
    offset: int = Query(0, ge=0),
    auth: AuthContext = Depends(require_membership),
) -> list[dict]:
    """Return only completed matches involving the authenticated user."""
    creator = aliased(User)
    opponent = aliased(User)
    async with SessionLocal() as session:
        rows = (
            await session.execute(
                select(ArenaMatch, creator, opponent)
                .join(creator, creator.id == ArenaMatch.creator_id)
                .outerjoin(opponent, opponent.id == ArenaMatch.opponent_id)
                .where(
                    ArenaMatch.chat_id == auth.chat_id,
                    ArenaMatch.status.in_(["finished", "cancelled", "refunded"]),
                    (ArenaMatch.creator_id == auth.user_id)
                    | (ArenaMatch.opponent_id == auth.user_id),
                )
                .order_by(ArenaMatch.finished_at.desc(), ArenaMatch.id.desc())
                .limit(limit)
                .offset(offset)
            )
        ).all()
    return [
        _serialize_arena_match_result(
            match,
            viewer_id=auth.user_id,
            viewer=creator if auth.user_id == match.creator_id else opponent,
            opponent=opponent if auth.user_id == match.creator_id else creator,
        )
        for match, creator, opponent in rows
    ]


@router.get("/api/v1/arena/leaderboard")
async def get_arena_leaderboard(
    limit: int = Query(50, ge=1, le=100),
    auth: AuthContext = Depends(require_membership),
) -> list[dict]:
    """Return the global all-time Arena rating leaderboard."""
    async with SessionLocal() as session:
        rows = (
            await session.execute(
                select(ArenaProfile, User)
                .join(User, User.id == ArenaProfile.user_id)
                .order_by(
                    ArenaProfile.rating.desc(),
                    ArenaProfile.wins.desc(),
                    ArenaProfile.total_matches.desc(),
                    ArenaProfile.user_id.asc(),
                )
                .limit(limit)
            )
        ).all()
    return [
        {
            "rank": index,
            "user_id": profile.user_id,
            "name": _display_name(user, profile.user_id),
            "rating": profile.rating,
            "wins": profile.wins,
            "total_matches": profile.total_matches,
            "is_me": profile.user_id == auth.user_id,
        }
        for index, (profile, user) in enumerate(rows, start=1)
    ]


@router.get("/api/v1/arena/matches/{match_id}/result")
async def get_arena_match_result(
    match_id: int, auth: AuthContext = Depends(require_membership)
) -> dict:
    """Return the final financial and rating summary for a participant only."""
    creator = aliased(User)
    opponent = aliased(User)
    async with SessionLocal() as session:
        row = (
            await session.execute(
                select(ArenaMatch, creator, opponent)
                .join(creator, creator.id == ArenaMatch.creator_id)
                .outerjoin(opponent, opponent.id == ArenaMatch.opponent_id)
                .where(ArenaMatch.id == match_id, ArenaMatch.chat_id == auth.chat_id)
            )
        ).first()
        if row is None:
            raise HTTPException(status_code=404, detail="Матч не найден")
        match, creator_user, opponent_user = row
        if auth.user_id not in {match.creator_id, match.opponent_id}:
            raise HTTPException(status_code=403, detail="Вы не участник этого матча")
        if match.status == "active":
            # Runtime normally settles in the request/scheduler. If the
            # terminal snapshot was persisted first but settlement was
            # interrupted, finish that same authoritative snapshot here
            # before exposing the result. This closes the DB-side part of the
            # terminal/result race without guessing an outcome.
            runtime_state = (match.replay_data or {}).get("runtime_state")
            if not isinstance(runtime_state, dict) or not runtime_state.get("terminal"):
                raise HTTPException(status_code=409, detail="Матч ещё не завершён")
            try:
                await arena_service.settle_match(
                    session, match.chat_id, match.id, runtime_state
                )
            except arena_service.ArenaServiceError:
                raise HTTPException(status_code=409, detail="Итог матча ещё рассчитывается")
        elif match.status not in {"finished", "cancelled", "refunded"}:
            raise HTTPException(status_code=409, detail="Матч ещё не завершён")
        profile = (
            await session.execute(
                select(ArenaProfile).where(ArenaProfile.user_id == auth.user_id)
            )
        ).scalar_one_or_none()
    return _serialize_arena_match_result(
        match,
        viewer_id=auth.user_id,
        viewer=creator_user if auth.user_id == match.creator_id else opponent_user,
        opponent=opponent_user if auth.user_id == match.creator_id else creator_user,
        rating=profile.rating if profile is not None else None,
    )


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


@router.get("/api/v1/arena/my-matches")
async def get_my_arena_matches(
    auth: AuthContext = Depends(require_membership),
) -> list[dict]:
    """Return only the authenticated user's unfinished matches."""
    async with SessionLocal() as session:
        matches = await arena_service.list_user_matches(session, auth.chat_id, auth.user_id)
    return [_serialize_match(match, viewer_id=auth.user_id) for match in matches]


@router.post("/api/v1/arena/matches", response_model=ArenaMatchResponse, status_code=201)
async def post_create_arena_match(
    body: CreateArenaMatchBody,
    request: Request,
    auth: AuthContext = Depends(require_membership),
) -> dict:
    await enforce_arena_rate_limit(
        request,
        auth,
        operation="create_match",
        limit=3,
        window_seconds=30,
    )
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


@router.post("/api/v1/arena/matches/{match_id}/accept", response_model=ArenaMatchResponse)
async def post_accept_arena_match(
    match_id: int,
    body: AcceptArenaMatchBody,
    request: Request,
    auth: AuthContext = Depends(require_membership),
) -> dict:
    await enforce_arena_rate_limit(
        request,
        auth,
        operation="accept_match",
        limit=5,
        window_seconds=30,
    )
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
