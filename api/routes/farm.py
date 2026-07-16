"""GET/POST /api/v1/farm* — тонкие роуты над `clicker_service` (FARM-01/02,
GACHA-02). `user_id`/`chat_id` берутся ТОЛЬКО из `AuthContext`
(`require_membership`) — тот же IDOR-контракт, что и `api/routes/games.py`
(T-04.2-02): Pydantic-модели тел запроса намеренно не содержат `user_id`.

Анти-чит тапов (T-04.2-07): `POST /farm/tap` прокидывает клиентский
`count`/`elapsed_ms` В `clicker_service.tap` AS-IS — реальный клэмп
(`MAX_CPS`, серверный `elapsed`) живёт внутри сервиса (04.1-04) и НЕ
ослабляется/не переопределяется здесь.

`ClickerError` -> 400 (недостаточно CP на апгрейд/конвертацию/покупку).
`economy_service.InsufficientFunds` -> 400 (`POST /farm/buy` списывает
ювики). `convert_cp`/`buy_cp` двигают ювики через `economy_service`
(FARM-01 AMM-мост, 04.1-05) — после успеха роут публикует свежий баланс в
`bal:{chat_id}` (`balance_events.publish_balance`), та же схема, что уже
установлена `api/routes/games.py` для казино (04.2-02) — без этого шага
живой SSE-баланс на экране фермы после конвертации/покупки CP не обновился
бы (тот же класс пробела, что уже однажды был найден и закрыт в 04.2-02).
"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Request
from pydantic import BaseModel
from pydantic import Field

from api.deps import AuthContext
from api.deps import require_membership
from bot.services import balance_events
from bot.services import clicker_service
from bot.services import economy_service
from common.db.session import SessionLocal

router = APIRouter()


class TapBody(BaseModel):
    count: int = Field(ge=1)
    elapsed_ms: int = Field(ge=0)


class ConvertBody(BaseModel):
    cp_in: int = Field(gt=0)
    ref_id: str


class BuyBody(BaseModel):
    hryvnia_in: int = Field(gt=0)
    ref_id: str


@router.get("/api/v1/farm")
async def get_farm(auth: AuthContext = Depends(require_membership)) -> dict:
    async with SessionLocal() as session:
        return await clicker_service.get_farm_state(session, auth.chat_id, auth.user_id)


@router.post("/api/v1/farm/tap")
async def post_tap(
    body: TapBody, auth: AuthContext = Depends(require_membership)
) -> dict:
    async with SessionLocal() as session:
        return await clicker_service.tap(
            session, auth.chat_id, auth.user_id, body.count, body.elapsed_ms
        )


@router.post("/api/v1/farm/upgrade/tap")
async def post_upgrade_tap(auth: AuthContext = Depends(require_membership)) -> dict:
    async with SessionLocal() as session:
        try:
            return await clicker_service.upgrade_tap(session, auth.chat_id, auth.user_id)
        except clicker_service.ClickerError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/v1/farm/upgrade/auto")
async def post_upgrade_auto(auth: AuthContext = Depends(require_membership)) -> dict:
    async with SessionLocal() as session:
        try:
            return await clicker_service.upgrade_auto(session, auth.chat_id, auth.user_id)
        except clicker_service.ClickerError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/v1/farm/convert")
async def post_convert(
    body: ConvertBody, request: Request, auth: AuthContext = Depends(require_membership)
) -> dict:
    async with SessionLocal() as session:
        try:
            result = await clicker_service.convert_cp(
                session, auth.chat_id, auth.user_id, body.cp_in, body.ref_id
            )
        except clicker_service.ClickerError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        balance = await economy_service.get_balance(session, auth.chat_id, auth.user_id)
        await balance_events.publish_balance(
            request.app.state.redis, auth.chat_id, auth.user_id, balance
        )
        result["user_balance_after"] = balance
        return result


@router.post("/api/v1/farm/buy")
async def post_buy(
    body: BuyBody, request: Request, auth: AuthContext = Depends(require_membership)
) -> dict:
    async with SessionLocal() as session:
        try:
            result = await clicker_service.buy_cp(
                session, auth.chat_id, auth.user_id, body.hryvnia_in, body.ref_id
            )
        except economy_service.InsufficientFunds as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        balance = await economy_service.get_balance(session, auth.chat_id, auth.user_id)
        await balance_events.publish_balance(
            request.app.state.redis, auth.chat_id, auth.user_id, balance
        )
        result["user_balance_after"] = balance
        return result


@router.get("/api/v1/farm/market")
async def get_market(auth: AuthContext = Depends(require_membership)) -> dict:
    async with SessionLocal() as session:
        return await clicker_service.get_market_state(session, auth.chat_id)
