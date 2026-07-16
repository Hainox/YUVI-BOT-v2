"""POST /api/v1/games/coinflip — тонкая обёртка над `casino_service.
play_coinflip` (CASINO-01, T-04.2-01/02/03).

`user_id`/`chat_id` берутся ТОЛЬКО из `AuthContext` (`require_membership`) —
Pydantic-модель тела запроса намеренно НЕ содержит поле `user_id` вовсе:
любое лишнее поле в JSON-теле (например поддельный `user_id` атакующего)
FastAPI/Pydantic молча игнорирует, роут никогда его не читает (IDOR,
T-04.2-02, доказано `tests/test_api_games.py::
test_coinflip_ignores_foreign_user_id_in_body_idor`).

Исключения `casino_service`/`economy_service` маппятся на HTTP:
`InvalidBet`/`InsufficientFunds` -> 400 (T-04.2-03, Pydantic `Field(ge=1)` —
первая линия защиты, `casino_service._validate_bet` — вторая, серверная);
`GameNotActive` -> 409. `DuplicateRound` — гонка конкурентных запросов с
одним `idem_key` (НЕ обычный replay: тот уже гасится внутри `_settle` без
исключения) — один повторный вызов `play_coinflip` обычно застаёт уже
завершённую конкурентную транзакцию и возвращает тот же успешный
сохранённый исход клиенту; если гонка не разрешилась и повторно — 409.

После успешного settle роут дочитывает свежий баланс и публикует его в
`bal:{chat_id}` через `balance_events.publish_balance` (D-02) — до 04.2-02
ни один вызывающий код фактически не звал `publish_balance` (сам примитив
и `GET /api/v1/events` существовали с Фазы 4, но были не подключены ни к
одному денежному пути), из-за чего SSE-канал никогда бы не сработал даже
после реального выигрыша/проигрыша. Ответ также несёт `user_balance_after`,
чтобы `lib/api.ts`'s balance-sniffing на клиенте сработал мгновенно, не
дожидаясь SSE round-trip (см. 04.2-RESEARCH.md `lib/api.ts` Code Example).
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
from bot.services import casino_service
from bot.services import economy_service
from common.db.session import SessionLocal

router = APIRouter()


class CoinflipBet(BaseModel):
    bet: int = Field(ge=1)
    choice: str
    idem_key: str


@router.post("/api/v1/games/coinflip")
async def post_coinflip(
    body: CoinflipBet, request: Request, auth: AuthContext = Depends(require_membership)
) -> dict:
    async with SessionLocal() as session:
        try:
            result = await _play(session, auth, body)
        except casino_service.DuplicateRound:
            try:
                result = await _play(session, auth, body)
            except casino_service.DuplicateRound as exc:
                raise HTTPException(status_code=409, detail="round in progress, retry") from exc
        except casino_service.GameNotActive as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except (casino_service.InvalidBet, economy_service.InsufficientFunds) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        balance = await economy_service.get_balance(session, auth.chat_id, auth.user_id)
        await balance_events.publish_balance(
            request.app.state.redis, auth.chat_id, auth.user_id, balance
        )
        result["user_balance_after"] = balance
        return result


async def _play(session, auth: AuthContext, body: CoinflipBet) -> dict:
    return await casino_service.play_coinflip(
        session, auth.chat_id, auth.user_id, body.bet, body.choice, body.idem_key
    )
