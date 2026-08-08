"""Minimal operator controls for unresolved Arena money.

Every route is guarded by the live Telegram admin check. The only destructive
operation exposed here is a full refund: a broken runtime must never be turned
into an invented winner by an operator endpoint.
"""

from __future__ import annotations

from datetime import datetime
from datetime import timedelta
from datetime import timezone

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Query
from fastapi import Request
from pydantic import BaseModel
from pydantic import Field
from sqlalchemy import func
from sqlalchemy import select

from api.deps import AuthContext
from api.deps import require_admin
from bot.services import arena_service
from bot.services import balance_events
from bot.services import economy_service
from common.db.session import SessionLocal
from common.models.arena import ArenaAdminAudit
from common.models.arena import ArenaFund
from common.models.arena import ArenaFundLedger
from common.models.arena import ArenaMatch

router = APIRouter()


class ArenaAdminRefundBody(BaseModel):
    reason: str = Field(min_length=1, max_length=200)
    idempotency_key: str = Field(min_length=1, max_length=120)


def _serialize_admin_match(match) -> dict:
    return {
        "id": match.id,
        "status": match.status,
        "settlement_status": match.settlement_status,
        "chat_id": match.chat_id,
        "creator_id": match.creator_id,
        "opponent_id": match.opponent_id,
        "creator_bet": match.creator_bet,
        "opponent_bet": match.opponent_bet,
        "created_at": match.created_at,
        "started_at": match.started_at,
        "finished_at": match.finished_at,
        "phase_count": match.phase_count,
        "result": match.match_result,
        "reason": match.result_reason,
    }


def _serialize_admin_ledger(row) -> dict:
    return {
        "id": row.id,
        "chat_id": row.chat_id,
        "amount": row.amount,
        "balance_after": row.balance_after,
        "kind": row.kind,
        "idempotency_key": row.idempotency_key,
        "match_id": row.match_id,
        "note": row.note,
        "created_at": row.created_at,
    }


@router.get("/api/v1/arena/admin/overview")
async def get_arena_admin_overview(
    auth: AuthContext = Depends(require_admin),
) -> dict:
    """Read-only operational snapshot for one chat.

    This endpoint deliberately does not infer winners or repair rows. It only
    exposes lifecycle counts and the Arena fund/ledger totals to an admin.
    """
    async with SessionLocal() as session:
        status_rows = (
            await session.execute(
                select(ArenaMatch.status, func.count())
                .where(ArenaMatch.chat_id == auth.chat_id)
                .group_by(ArenaMatch.status)
            )
        ).all()
        fund_balance = (
            await session.execute(
                select(ArenaFund.balance).where(ArenaFund.chat_id == auth.chat_id)
            )
        ).scalar_one_or_none()
        ledger_count, ledger_total = (
            await session.execute(
                select(
                    func.count(ArenaFundLedger.id),
                    func.coalesce(func.sum(ArenaFundLedger.amount), 0),
                ).where(ArenaFundLedger.chat_id == auth.chat_id)
            )
        ).one()

    statuses = {status: int(count) for status, count in status_rows}
    unresolved = sum(statuses.get(status, 0) for status in ("waiting", "accepting", "active"))
    return {
        "chat_id": auth.chat_id,
        "matches": statuses,
        "unresolved_matches": unresolved,
        "settlement_pending_matches": statuses.get("active", 0),
        "fund": {
            "balance": int(fund_balance or 0),
            "ledger_entries": int(ledger_count or 0),
            "ledger_total": int(ledger_total or 0),
        },
    }


@router.get("/api/v1/arena/admin/fund-ledger")
async def get_arena_admin_fund_ledger(
    limit: int = Query(100, ge=1, le=200),
    offset: int = Query(0, ge=0),
    auth: AuthContext = Depends(require_admin),
) -> list[dict]:
    """Return the append-only Arena fund ledger for operator reconciliation."""
    async with SessionLocal() as session:
        rows = (
            await session.execute(
                select(ArenaFundLedger)
                .where(ArenaFundLedger.chat_id == auth.chat_id)
                .order_by(ArenaFundLedger.created_at.desc(), ArenaFundLedger.id.desc())
                .limit(limit)
                .offset(offset)
            )
        ).scalars().all()
    return [_serialize_admin_ledger(row) for row in rows]


@router.get("/api/v1/arena/admin/stuck")
async def get_stuck_arena_matches(
    older_than_seconds: int = Query(120, ge=1, le=86400),
    limit: int = Query(100, ge=1, le=200),
    auth: AuthContext = Depends(require_admin),
) -> list[dict]:
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=older_than_seconds)
    async with SessionLocal() as session:
        matches = await arena_service.list_stuck_matches(
            session, auth.chat_id, older_than=cutoff, limit=limit
        )
    return [_serialize_admin_match(match) for match in matches]


@router.get("/api/v1/arena/admin/audit")
async def get_arena_admin_audit(
    limit: int = Query(100, ge=1, le=200),
    auth: AuthContext = Depends(require_admin),
) -> list[dict]:
    async with SessionLocal() as session:
        rows = (
            await session.execute(
                select(ArenaAdminAudit)
                .where(ArenaAdminAudit.chat_id == auth.chat_id)
                .order_by(ArenaAdminAudit.created_at.desc())
                .limit(limit)
            )
        ).scalars().all()
    return [
        {
            "id": row.id,
            "operation": row.operation,
            "admin_id": row.admin_id,
            "match_id": row.match_id,
            "amount": row.amount,
            "reason": row.reason,
            "result": row.result,
            "details": row.details,
            "created_at": row.created_at,
        }
        for row in rows
    ]


@router.post("/api/v1/arena/admin/matches/{match_id}/refund")
async def post_arena_admin_refund(
    match_id: int,
    body: ArenaAdminRefundBody,
    request: Request,
    auth: AuthContext = Depends(require_admin),
) -> dict:
    async with SessionLocal() as session:
        try:
            match = await arena_service.admin_refund_match(
                session,
                auth.chat_id,
                match_id,
                auth.user_id,
                body.reason,
                body.idempotency_key,
            )
        except arena_service.MatchNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except arena_service.InvalidMatch as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except arena_service.MatchConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

        for user_id in (match.creator_id, match.opponent_id):
            if user_id is None:
                continue
            balance = await economy_service.get_balance(session, match.chat_id, user_id)
            await balance_events.publish_balance(
                request.app.state.redis, match.chat_id, user_id, balance
            )
    return _serialize_admin_match(match)
