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

`bank_capped` (bool, все три игры этого файла — coinflip/dice/roulette):
D-06 (`economy_service.pay_from_bank`) урезает выплату до текущего остатка
`chat_bank` — на свежем чате с пустым банком выигрыш по факту RNG может
выплатить МЕНЬШЕ честного payout (в худшем случае вплоть до `bet`, т.е.
баланс игрока не меняется вовсе, хотя раунд был выигран). Без явного флага
это выглядит для игрока как "баланс не обновился после победы" (реальный
инцидент верификации 04.2-02: /me до и после раунда — 1000 и 1000, банк
чата был 0, весь выигрыш ушёл на компенсацию собственной же ставки).
Флаг вычисляется ЗДЕСЬ (роут), не в `casino_service.py` — модуль settle-ядра
общий для всех игр казино с разными формулами мультипликатора, трогать его
ради одного UI-флага не нужно. dice/roulette (04.2-03) повторяют тот же
паттерн, что и coinflip (04.2-02), пересчитывая свою "честную" формулу
выплаты по данным, уже доступным роуту (target/direction для dice,
bet_type для roulette), а не дублируя приватную `compute()`-логику
`casino_service`.

POST /games/dice и POST /games/roulette (04.2-03) — тот же тонкий паттерн,
что и coinflip выше: `casino_service.play_dice`/`play_roulette` уже несут
всю валидацию/RNG/settle-логику (04.1-01), роут только парсит тело,
прокидывает `auth.user_id`/`auth.chat_id`, маппит исключения на HTTP и
публикует баланс.
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


class DiceBet(BaseModel):
    bet: int = Field(ge=1)
    target: int
    direction: str
    idem_key: str


class RouletteBet(BaseModel):
    bet: int = Field(ge=1)
    bet_type: str
    bet_value: int | str
    idem_key: str


# bet_type -> "честный" (без D-06 капа) множитель выплаты, для bank_capped
# (см. модульный докстринг выше). Держится рядом с роутом, не в
# casino_service.py — та же причина, что у coinflip's fair_payout ниже.
_ROULETTE_FAIR_MULT: dict[str, int] = {
    "number": casino_service.ROULETTE_NUMBER_MULT,
    "color": casino_service.ROULETTE_EVEN_MULT,
    "parity": casino_service.ROULETTE_EVEN_MULT,
    "half": casino_service.ROULETTE_EVEN_MULT,
    "dozen": casino_service.ROULETTE_DOZEN_MULT,
}


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

        outcome = result.get("outcome") or {}
        if outcome.get("won"):
            fair_payout = int(result["bet"] * casino_service.COINFLIP_MULT)
            result["bank_capped"] = result["payout"] < fair_payout

        return result


async def _play(session, auth: AuthContext, body: CoinflipBet) -> dict:
    return await casino_service.play_coinflip(
        session, auth.chat_id, auth.user_id, body.bet, body.choice, body.idem_key
    )


@router.post("/api/v1/games/dice")
async def post_dice(
    body: DiceBet, request: Request, auth: AuthContext = Depends(require_membership)
) -> dict:
    async with SessionLocal() as session:
        try:
            result = await _play_dice(session, auth, body)
        except casino_service.DuplicateRound:
            try:
                result = await _play_dice(session, auth, body)
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

        outcome = result.get("outcome") or {}
        if outcome.get("won"):
            win_prob = (
                (body.target - 1) / 100 if body.direction == "under" else (100 - body.target) / 100
            )
            fair_payout = int(result["bet"] * (1 - casino_service.DICE_HOUSE_EDGE) / win_prob)
            result["bank_capped"] = result["payout"] < fair_payout

        return result


async def _play_dice(session, auth: AuthContext, body: DiceBet) -> dict:
    return await casino_service.play_dice(
        session, auth.chat_id, auth.user_id, body.bet, body.target, body.direction, body.idem_key
    )


@router.post("/api/v1/games/roulette")
async def post_roulette(
    body: RouletteBet, request: Request, auth: AuthContext = Depends(require_membership)
) -> dict:
    async with SessionLocal() as session:
        try:
            result = await _play_roulette(session, auth, body)
        except casino_service.DuplicateRound:
            try:
                result = await _play_roulette(session, auth, body)
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

        outcome = result.get("outcome") or {}
        if outcome.get("won"):
            fair_mult = _ROULETTE_FAIR_MULT[body.bet_type]
            fair_payout = int(result["bet"] * fair_mult)
            result["bank_capped"] = result["payout"] < fair_payout

        return result


async def _play_roulette(session, auth: AuthContext, body: RouletteBet) -> dict:
    return await casino_service.play_roulette(
        session, auth.chat_id, auth.user_id, body.bet, body.bet_type, body.bet_value, body.idem_key
    )
