"""GET/POST /api/v1/markets* — тонкие роуты над `bot.services.markets_service`
(BET-01/02/03 UI-надстройка, D-04, T-04.2-01/02/03).

`user_id`/`chat_id` берутся ТОЛЬКО из `AuthContext` (`require_membership`) —
тот же IDOR-паттерн, что `games.py`/`farm.py`/`duel.py`: Pydantic-модель тела
запроса `PlaceBetBody` намеренно НЕ содержит поле `user_id` — любое лишнее
поле в JSON-теле (например поддельный `user_id` атакующего) FastAPI/Pydantic
молча игнорирует, роут никогда его не читает.

Исключения `markets_service`/`economy_service` маппятся на HTTP:
`InvalidMarketArg` -> 400, `MarketClosed` -> 409, `MarketNotFound` -> 404,
`economy_service.InsufficientFunds` -> 400 (RESEARCH.md Route-to-Service
Mapping). `place_bet` возвращает `None` на повтор `ref_id` (идемпотентный
no-op, а НЕ ошибка) — роут отвечает тем же успешным телом с
`replayed: true`, деньги повторно не двигаются.

`GET /api/v1/markets/portfolio` регистрируется ДО `GET /api/v1/markets/
{market_id}` в этом файле — иначе Starlette матчил бы литеральный путь
"portfolio" как параметр `{market_id}` (маршруты матчатся в порядке
регистрации, форма уже установленного порядка в `api/routes/duel.py`).

После успешной ставки роут дочитывает свежий баланс и публикует его в
`bal:{chat_id}` через `balance_events.publish_balance` (D-02) — тот же
паттерн, что уже установлен для games.py/farm.py/gacha.py/duel.py (04.2-02/
04/05/06): без этого шага живой SSE-баланс на экране рынков после ставки не
обновился бы. Ответ также несёт `user_balance_after` для мгновенного
balance-sniffing на клиенте (`lib/api.ts`).
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
from bot.services import economy_service
from bot.services import markets_service
from common.db.session import SessionLocal

router = APIRouter()


class PlaceBetBody(BaseModel):
    option_position: int = Field(ge=1)
    amount: int = Field(ge=1)
    ref_id: str


@router.get("/api/v1/markets")
async def list_markets(auth: AuthContext = Depends(require_membership)) -> list[dict]:
    async with SessionLocal() as session:
        return await markets_service.get_open_markets(session, auth.chat_id)


@router.get("/api/v1/markets/portfolio")
async def get_portfolio(auth: AuthContext = Depends(require_membership)) -> list[dict]:
    async with SessionLocal() as session:
        return await markets_service.get_user_portfolio(session, auth.chat_id, auth.user_id)


@router.get("/api/v1/markets/{market_id}")
async def get_market(market_id: int, auth: AuthContext = Depends(require_membership)) -> dict:
    async with SessionLocal() as session:
        try:
            return await markets_service.get_market_detail(session, auth.chat_id, market_id)
        except markets_service.MarketNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/api/v1/markets/{market_id}/bets")
async def post_bet(
    market_id: int,
    body: PlaceBetBody,
    request: Request,
    auth: AuthContext = Depends(require_membership),
) -> dict:
    async with SessionLocal() as session:
        try:
            bet = await markets_service.place_bet(
                session,
                auth.chat_id,
                market_id,
                auth.user_id,
                body.option_position,
                body.amount,
                body.ref_id,
            )
        except markets_service.MarketNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except markets_service.MarketClosed as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except (markets_service.InvalidMarketArg, economy_service.InsufficientFunds) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        balance = await economy_service.get_balance(session, auth.chat_id, auth.user_id)
        await balance_events.publish_balance(
            request.app.state.redis, auth.chat_id, auth.user_id, balance
        )

        if bet is None:
            return {
                "replayed": True,
                "market_id": market_id,
                "user_balance_after": balance,
            }

        return {
            "replayed": False,
            "bet_id": bet.id,
            "market_id": market_id,
            "option_position": body.option_position,
            "amount": bet.amount,
            "user_balance_after": balance,
        }
