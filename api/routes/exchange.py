"""GET/POST /api/v1/exchange* — тонкие роуты над `bot.services.exchange_service`
(EXCHANGE-01). `user_id`/`chat_id` берутся ТОЛЬКО из `AuthContext`
(`require_membership`) — тот же IDOR-контракт, что и остальные роуты этого
пакета (`api/routes/twin.py`/`farm.py`/`duel.py`, T-04.2-01/02):
Pydantic-тела запросов намеренно не содержат ни `user_id`, ни `chat_id`.

`GET /api/v1/exchange/mine` — дополнительный роут сверх минимального набора
из плана (BET-01-стиль `markets.py::get_portfolio`): без него Mini App не
смог бы показать участнику его СОБСТВЕННЫЕ листинги (claimed-листинги уже не
входят в открытый список — `GET /api/v1/exchange` отдаёт только status=open,
форма `markets_service.get_open_markets`) для cancel/confirm-действий.
Конфликта регистрации с `GET /api/v1/exchange/{id}` нет — такого роута в
этом файле не существует (см. ниже).

Админский force-cancel/force-release (споры по зависшим claimed-листингам)
сюда НЕ вынесены — по объёму задачи это остаётся командами бота
(`bot/handlers/exchange.py`, гейт `admin_service.is_chat_admin`, форма
`bot/handlers/duel.py::unmute_command`), не Mini App API.

Маппинг исключений (та же таблица, что `duel.py`/`markets.py`):
`exchange_service.ListingNotFound` -> 404, `exchange_service.ExchangeError`
(включая `ListingAlreadyResolved` — replay того же ref_id) -> 400,
`economy_service.InsufficientFunds` -> 400.

После каждого денежного действия (create/cancel/confirm) роут дочитывает
свежий баланс затронутого участника и публикует его в `bal:{chat_id}` через
`balance_events.publish_balance` (D-02) — тот же паттерн, что уже установлен
`api/routes/duel.py`/`farm.py`/`markets.py`: без этого шага живой SSE-баланс
на экране биржи не обновился бы после эскроу/рефанда/релиза (тот же класс
пробела, что уже однажды был найден и закрыт в 04.2-02 для другого роута).
claim_listing денег не двигает (см. exchange_service докстринг) — баланс не
публикуется.
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
from bot.services import exchange_service
from common.db.session import SessionLocal

router = APIRouter()


class CreateListingBody(BaseModel):
    yuvik_amount: int = Field(ge=1)
    want_description: str
    ref_id: str


class ConfirmBody(BaseModel):
    ref_id: str


async def _publish_balance(session, redis_client, chat_id: int, user_id: int) -> None:
    balance = await economy_service.get_balance(session, chat_id, user_id)
    await balance_events.publish_balance(redis_client, chat_id, user_id, balance)


@router.get("/api/v1/exchange")
async def list_open_listings(auth: AuthContext = Depends(require_membership)) -> list[dict]:
    async with SessionLocal() as session:
        return await exchange_service.get_open_listings(session, auth.chat_id)


@router.get("/api/v1/exchange/mine")
async def list_my_listings(auth: AuthContext = Depends(require_membership)) -> list[dict]:
    async with SessionLocal() as session:
        return await exchange_service.get_my_listings(session, auth.chat_id, auth.user_id)


@router.post("/api/v1/exchange")
async def post_create_listing(
    body: CreateListingBody, request: Request, auth: AuthContext = Depends(require_membership)
) -> dict:
    async with SessionLocal() as session:
        try:
            listing = await exchange_service.create_listing(
                session,
                auth.chat_id,
                auth.user_id,
                body.yuvik_amount,
                body.want_description,
                body.ref_id,
            )
        except exchange_service.ExchangeError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except economy_service.InsufficientFunds as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        await _publish_balance(session, request.app.state.redis, auth.chat_id, auth.user_id)

        return {
            "id": listing.id,
            "status": listing.status,
            "yuvik_amount": listing.yuvik_amount,
            "want_description": listing.want_description,
        }


@router.post("/api/v1/exchange/{listing_id}/claim")
async def post_claim_listing(
    listing_id: int, auth: AuthContext = Depends(require_membership)
) -> dict:
    async with SessionLocal() as session:
        try:
            return await exchange_service.claim_listing(session, auth.chat_id, listing_id, auth.user_id)
        except exchange_service.ListingNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except exchange_service.ExchangeError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/v1/exchange/{listing_id}/cancel")
async def post_cancel_listing(
    listing_id: int, request: Request, auth: AuthContext = Depends(require_membership)
) -> dict:
    async with SessionLocal() as session:
        try:
            result = await exchange_service.cancel_listing(session, auth.chat_id, listing_id, auth.user_id)
        except exchange_service.ListingNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except exchange_service.ExchangeError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        if result.get("status") == exchange_service.STATUS_CANCELLED:
            await _publish_balance(session, request.app.state.redis, auth.chat_id, auth.user_id)

        return result


@router.post("/api/v1/exchange/{listing_id}/confirm")
async def post_confirm_listing(
    listing_id: int,
    body: ConfirmBody,
    request: Request,
    auth: AuthContext = Depends(require_membership),
) -> dict:
    async with SessionLocal() as session:
        try:
            result = await exchange_service.confirm_fulfillment(
                session, auth.chat_id, listing_id, auth.user_id, body.ref_id
            )
        except exchange_service.ListingNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except exchange_service.ExchangeError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        claimed_by_user_id = result.get("claimed_by_user_id")
        if result.get("status") == exchange_service.STATUS_FULFILLED and claimed_by_user_id is not None:
            await _publish_balance(session, request.app.state.redis, auth.chat_id, claimed_by_user_id)

        return result
