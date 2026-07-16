"""GET /api/v1/stats — personal read-only dashboard (D-05, CASINO-02).

Composes existing `economy_service`/`stats_service`/`clicker_service` read
functions into one screen — the "Статистика" hub tile. This is a
compose-don't-duplicate route (same principle as `card_service.build_card`
in Phase 2, per `04.2-RESEARCH.md`): it does NOT re-derive balance, chat
economy summary, streak, peak day, participant ranking, or farm state — it
calls the exact functions those already-shipped screens (`/me`, `/economy`,
`/mystats`, `/chatstats`, `/who`, farm screen) already use.

Two narrow read-only aggregations live in this module because no existing
service function exposes them yet (no write path is added or touched):

- `_casino_round_stats`: rounds played / net win-loss / biggest single win,
  read directly from `CasinoGame` (the same rows the History feed's
  `economy_tx` `casino_bet`/`casino_payout` entries originate from — reading
  `CasinoGame` directly avoids re-pairing two separate `economy_tx` rows per
  round back into one round).
- `_farm_total_converted`: sum of `economy_tx` rows with
  `kind="farm_convert"` — mirrors the write side in
  `bot.services.clicker_service.convert_cp`, which has no existing read
  counterpart.

Auth: `require_membership` (T-04.2-01); `user_id`/`chat_id` come ONLY from
`AuthContext`, never from query/body (IDOR, T-04.2-02) — the dashboard is
always the authenticated user's own data.

Empty/partial data (brand-new user, zero rounds/messages) renders as
`0`/`null`, never an error — the frontend renders `—` for these per
`04.2-UI-SPEC.md`.
"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi import Depends
from sqlalchemy import func
from sqlalchemy import select

from api.deps import AuthContext
from api.deps import require_membership
from bot.services import clicker_service
from bot.services import economy_service
from bot.services import stats_service
from common.db.session import SessionLocal
from common.models.casino_game import CasinoGame
from common.models.economy_tx import EconomyTx

router = APIRouter()

# Single-chat project (PROJECT.md constraint) — a generous cap, not a real
# pagination boundary, so message-rank lookup sees every participant.
_RANK_SCAN_LIMIT = 10_000


async def _casino_round_stats(session, chat_id: int, user_id: int) -> dict:
    """Read-only aggregation over `CasinoGame` — rounds played, net
    win/loss (signed), and the single biggest win (amount + game name).
    No existing service function exposes this; no write happens here."""
    totals_stmt = select(
        func.count(),
        func.coalesce(func.sum(CasinoGame.payout - CasinoGame.bet), 0),
    ).where(
        CasinoGame.chat_id == chat_id,
        CasinoGame.user_id == user_id,
        CasinoGame.status == "settled",
    )
    rounds_played, net_result = (await session.execute(totals_stmt)).one()

    biggest_stmt = (
        select(CasinoGame.game, CasinoGame.payout)
        .where(
            CasinoGame.chat_id == chat_id,
            CasinoGame.user_id == user_id,
            CasinoGame.status == "settled",
            CasinoGame.payout > 0,
        )
        .order_by(CasinoGame.payout.desc())
        .limit(1)
    )
    biggest_row = (await session.execute(biggest_stmt)).first()
    biggest_win = (
        {"amount": int(biggest_row.payout), "game": biggest_row.game}
        if biggest_row is not None
        else None
    )

    return {
        "rounds_played": int(rounds_played),
        "net_result": int(net_result),
        "biggest_win": biggest_win,
    }


async def _farm_total_converted(session, chat_id: int, user_id: int) -> int:
    """Read-only aggregation over `economy_tx` kind="farm_convert" — the
    write side lives in `clicker_service.convert_cp`; no service function
    exposes a running total, so this route reads it directly (no writes)."""
    stmt = select(func.coalesce(func.sum(EconomyTx.amount), 0)).where(
        EconomyTx.chat_id == chat_id,
        EconomyTx.user_id == user_id,
        EconomyTx.kind == "farm_convert",
    )
    return int((await session.execute(stmt)).scalar_one())


async def _message_rank(session, chat_id: int, user_id: int) -> int | None:
    """1-based rank by total messages — reuses
    `stats_service.get_top_participants` (same function already powering
    `/who` and the Лидерборд screen), no new SQL. `None` if the user has no
    messages at all (brand-new user)."""
    top = await stats_service.get_top_participants(session, chat_id, limit=_RANK_SCAN_LIMIT)
    for idx, row in enumerate(top, start=1):
        if row["user_id"] == user_id:
            return idx
    return None


@router.get("/api/v1/stats")
async def get_stats(auth: AuthContext = Depends(require_membership)) -> dict:
    async with SessionLocal() as session:
        balance = await economy_service.get_balance(session, auth.chat_id, auth.user_id)
        chat_summary = await economy_service.get_chat_summary(session, auth.chat_id)
        total_in_circulation = chat_summary["total_in_circulation"]
        bank_share_pct = (
            (balance / total_in_circulation * 100) if total_in_circulation > 0 else None
        )

        casino = await _casino_round_stats(session, auth.chat_id, auth.user_id)

        streak = await stats_service.get_streak(session, auth.chat_id, auth.user_id)
        peak_day = await stats_service.get_peak_day(session, auth.chat_id)
        message_rank = await _message_rank(session, auth.chat_id, auth.user_id)

        farm_state = await clicker_service.get_farm_state(session, auth.chat_id, auth.user_id)
        total_converted = await _farm_total_converted(session, auth.chat_id, auth.user_id)

    return {
        "balance": balance,
        "bank_share_pct": bank_share_pct,
        "casino": casino,
        "activity": {
            "streak": streak,
            "peak_day": (
                {"date": peak_day[0].isoformat(), "message_count": peak_day[1]}
                if peak_day is not None
                else None
            ),
            "message_rank": message_rank,
        },
        "farm": {
            "cp_per_sec": farm_state["cp_per_sec"],
            "total_converted": total_converted,
        },
    }
