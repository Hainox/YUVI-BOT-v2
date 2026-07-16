"""GET /api/v1/me — баланс аутентифицированного пользователя (ECON-01, D-01).

Тонкая обёртка над `bot.services.economy_service.get_balance` — `user_id`/
`chat_id` берутся ТОЛЬКО из `AuthContext` (`require_membership`), никогда из
query/тела запроса (IDOR, T-04.2-01/T-04.2-02).

04.2-08 расширяет этот же модуль (без изменений в GET /me выше) четырьмя
социально-экономическими роутами CASINO-02, все тонкие обёртки над уже
существующими `economy_service`-функциями:

- `GET /api/v1/leaderboard` -> `economy_service.get_leaderboard`
- `POST /api/v1/transfer` -> `economy_service.transfer_with_fee`; тело
  запроса (`TransferBody`) НЕ содержит поле отправителя вовсе (та же форма,
  что `CoinflipBet`/`CreateDuelBody` в `games.py`/`duel.py`) — `from_user`
  берётся ИСКЛЮЧИТЕЛЬНО из `auth.user_id`, любое постороннее поле в JSON-теле
  Pydantic молча игнорирует (IDOR, T-04.2-02).
- `GET /api/v1/economy` -> `economy_service.get_chat_summary`
- `GET /api/v1/history` -> `economy_service.get_transactions` для
  `auth.user_id` (T-04.2-12: лента истории всегда скопирована на
  аутентифицированного пользователя, никогда не принимает чужой `user_id`
  из query).

Маппинг исключений (та же таблица, что `games.py`/`duel.py`):
`InvalidArgument`/`InsufficientFunds` -> 400.
"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Query
from fastapi import Request
from pydantic import BaseModel
from pydantic import Field

from api.deps import AuthContext
from api.deps import require_membership
from bot.services import balance_events
from bot.services import economy_service
from common.db.session import SessionLocal

router = APIRouter()


class TransferBody(BaseModel):
    to_user_id: int
    amount: int = Field(ge=1)
    ref_id: str


@router.get("/api/v1/me")
async def get_me(auth: AuthContext = Depends(require_membership)) -> dict:
    async with SessionLocal() as session:
        balance = await economy_service.get_balance(session, auth.chat_id, auth.user_id)
    return {"balance": balance}


@router.get("/api/v1/leaderboard")
async def get_leaderboard(auth: AuthContext = Depends(require_membership)) -> list[dict]:
    async with SessionLocal() as session:
        return await economy_service.get_leaderboard(session, auth.chat_id)


@router.post("/api/v1/transfer")
async def post_transfer(
    body: TransferBody, request: Request, auth: AuthContext = Depends(require_membership)
) -> dict:
    async with SessionLocal() as session:
        try:
            await economy_service.transfer_with_fee(
                session, auth.chat_id, auth.user_id, body.to_user_id, body.amount, body.ref_id
            )
        except (economy_service.InvalidArgument, economy_service.InsufficientFunds) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        balance = await economy_service.get_balance(session, auth.chat_id, auth.user_id)
        await balance_events.publish_balance(request.app.state.redis, auth.chat_id, auth.user_id, balance)

        # WR-02 (04.2-REVIEW): transfer_with_fee also credits to_user_id — the
        # recipient's balance genuinely changes, so it must be published too
        # (same "publish to every affected user" pattern as duel.py's
        # post_accept_duel, which publishes for both winner_id and loser_id).
        recipient_balance = await economy_service.get_balance(session, auth.chat_id, body.to_user_id)
        await balance_events.publish_balance(
            request.app.state.redis, auth.chat_id, body.to_user_id, recipient_balance
        )

        return {"status": "ok", "balance": balance}


@router.get("/api/v1/economy")
async def get_economy(auth: AuthContext = Depends(require_membership)) -> dict:
    async with SessionLocal() as session:
        return await economy_service.get_chat_summary(session, auth.chat_id)


@router.get("/api/v1/history")
async def get_history(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    auth: AuthContext = Depends(require_membership),
) -> list[dict]:
    async with SessionLocal() as session:
        return await economy_service.get_transactions(
            session, auth.chat_id, user_id=auth.user_id, limit=limit, offset=offset
        )
