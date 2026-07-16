"""POST /api/v1/gacha/roll + GET /api/v1/gacha/collection — тонкие роуты над
`bot.services.gacha_service` (GACHA-01, визуальная часть GACHA-03).
`user_id`/`chat_id` берутся ТОЛЬКО из `AuthContext` (`require_membership`) —
тот же IDOR-контракт, что и `api/routes/games.py`/`farm.py` (T-04.2-02):
Pydantic-тело ролла намеренно не несёт `user_id`.

`count` — простой `int` (НЕ Pydantic `Literal[1, 10]`): валидация "1 либо
10" целиком делегирована `gacha_service.roll` (`GachaError` -> 400) — та же
серверная проверка, что уже полностью протестирована в `test_gacha_service.
py`, роут её не дублирует. Идемпотентность по `ref_id` — повтор возвращает
`replay: True`, деньги повторно не двигаются (`gacha_service.roll`, D-03).

После успешного ролла роут дочитывает баланс и публикует его в
`bal:{chat_id}` (`balance_events.publish_balance`) — тот же паттерн, что
`games.py`/`farm.py` (D-02).

`GET /gacha/collection` — чистый read: `gacha_service.get_collection`
(коллекция персонажей игрока, обогащённая `gacha_catalog.CATALOG`, plus
pity, rate-up баннер). Роут не содержит ни одного SQL-запроса и не
ссылается на ORM-модель коллекции напрямую (доказано
`tests/test_api_gacha.py::test_gacha_route_composes_service_no_raw_sql`).
"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Request
from pydantic import BaseModel

from api.deps import AuthContext
from api.deps import require_membership
from bot.services import balance_events
from bot.services import economy_service
from bot.services import gacha_service
from common.db.session import SessionLocal

router = APIRouter()


class RollBody(BaseModel):
    count: int
    ref_id: str


@router.post("/api/v1/gacha/roll")
async def post_roll(
    body: RollBody, request: Request, auth: AuthContext = Depends(require_membership)
) -> dict:
    async with SessionLocal() as session:
        try:
            result = await gacha_service.roll(
                session, auth.chat_id, auth.user_id, body.count, body.ref_id
            )
        except gacha_service.GachaError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except economy_service.InsufficientFunds as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        balance = await economy_service.get_balance(session, auth.chat_id, auth.user_id)
        await balance_events.publish_balance(
            request.app.state.redis, auth.chat_id, auth.user_id, balance
        )
        result["user_balance_after"] = balance
        return result


@router.get("/api/v1/gacha/collection")
async def get_collection(auth: AuthContext = Depends(require_membership)) -> dict:
    async with SessionLocal() as session:
        return await gacha_service.get_collection(session, auth.chat_id, auth.user_id)
