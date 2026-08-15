"""POST /api/v1/autobattle (create) + /autobattle/{id}/accept + /autobattle/
{id}/decline + /autobattle/{id}/cancel + GET /api/v1/autobattle/power — thin
routes over `bot.services.autobattle_service` (GACHA-06). Форма
`api/routes/duel.py` (читай его докстринг за полным разбором) — `user_id`/
`chat_id` берутся ТОЛЬКО из `AuthContext` (`require_membership`), никогда из
тела запроса (IDOR, тот же контракт, что Duel: `CreateAutoBattleBody.
opponent_id` называет цель вызова, не действующего игрока).

В отличие от Duel здесь нет побочного эффекта мута (autobattle_service не
возвращает loser_id для мута — исход только двигает деньги), поэтому роут
проще: нет `api.duel_mute`-аналога.

Exception mapping: `AutoBattleNotFound` -> 404 (подкласс `AutoBattleError`,
проверяется первым); `AutoBattleError` (включая `AutoBattleAlreadyResolved`
— реплей с тем же `ref_id`) -> 400; `economy_service.InsufficientFunds` ->
400.

После каждого денежного вызова роут дочитывает и публикует затронутые
балансы в `bal:{chat_id}` через `balance_events.publish_balance` (D-02) —
тот же паттерн, что `duel.py`/`games.py`/`farm.py`/`gacha.py`.

`GET /autobattle/power` — чистый read `autobattle_service.team_power`
текущего игрока: превью силы коллекции ДО вызова на автобитву (UX-нужда,
которой у Duel нет — там исход честный 50/50, здесь сила реально влияет,
игрок должен видеть её перед тем, как ставить)."""

from __future__ import annotations

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Request
from pydantic import BaseModel
from pydantic import Field

from api.deps import AuthContext
from api.deps import require_membership
from bot.services import autobattle_service
from bot.services import balance_events
from bot.services import economy_service
from common.db.session import SessionLocal

router = APIRouter()


class CreateAutoBattleBody(BaseModel):
    opponent_id: int
    stake: int = Field(ge=1)
    ref_id: str


class AcceptAutoBattleBody(BaseModel):
    ref_id: str


async def _publish_balance(session, redis_client, chat_id: int, user_id: int) -> None:
    balance = await economy_service.get_balance(session, chat_id, user_id)
    await balance_events.publish_balance(redis_client, chat_id, user_id, balance)


@router.get("/api/v1/autobattle/power")
async def get_power(auth: AuthContext = Depends(require_membership)) -> dict:
    async with SessionLocal() as session:
        power = await autobattle_service.team_power(session, auth.chat_id, auth.user_id)
        return {"power": power}


@router.post("/api/v1/autobattle")
async def post_create_autobattle(
    body: CreateAutoBattleBody, request: Request, auth: AuthContext = Depends(require_membership)
) -> dict:
    async with SessionLocal() as session:
        try:
            battle = await autobattle_service.create_autobattle(
                session, auth.chat_id, auth.user_id, body.opponent_id, body.stake, body.ref_id
            )
        except autobattle_service.AutoBattleError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except economy_service.InsufficientFunds as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        await _publish_balance(session, request.app.state.redis, auth.chat_id, auth.user_id)

        return {
            "autobattle_id": battle.id,
            "status": battle.status,
            "challenger_id": battle.challenger_id,
            "opponent_id": battle.opponent_id,
            "stake": battle.stake,
        }


@router.post("/api/v1/autobattle/{autobattle_id}/accept")
async def post_accept_autobattle(
    autobattle_id: int,
    body: AcceptAutoBattleBody,
    request: Request,
    auth: AuthContext = Depends(require_membership),
) -> dict:
    async with SessionLocal() as session:
        try:
            result = await autobattle_service.accept_autobattle(
                session, auth.chat_id, autobattle_id, auth.user_id, body.ref_id
            )
        except autobattle_service.AutoBattleNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except autobattle_service.AutoBattleError as exc:
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

        return result


@router.post("/api/v1/autobattle/{autobattle_id}/decline")
async def post_decline_autobattle(
    autobattle_id: int, request: Request, auth: AuthContext = Depends(require_membership)
) -> dict:
    async with SessionLocal() as session:
        try:
            result = await autobattle_service.decline_autobattle(
                session, auth.chat_id, autobattle_id, auth.user_id
            )
        except autobattle_service.AutoBattleNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except autobattle_service.AutoBattleError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        if result.get("status") == "declined":
            battle = await session.get(autobattle_service.AutoBattle, autobattle_id)
            await _publish_balance(session, request.app.state.redis, auth.chat_id, battle.challenger_id)

        return result


@router.post("/api/v1/autobattle/{autobattle_id}/cancel")
async def post_cancel_autobattle(
    autobattle_id: int, request: Request, auth: AuthContext = Depends(require_membership)
) -> dict:
    async with SessionLocal() as session:
        try:
            result = await autobattle_service.cancel_autobattle(
                session, auth.chat_id, autobattle_id, auth.user_id
            )
        except autobattle_service.AutoBattleNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except autobattle_service.AutoBattleError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        if result.get("status") == "cancelled":
            await _publish_balance(session, request.app.state.redis, auth.chat_id, auth.user_id)

        return result
