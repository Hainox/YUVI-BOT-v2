"""GET/POST /api/v1/shop/* — тонкие роуты над `social_service.do_*` (SHOP-01:
poke/hug/joke_order/roast) для соцмагазина в Mini App. `actor_id`/`chat_id` —
ТОЛЬКО из `AuthContext` (`require_membership`), тот же IDOR-контракт, что и
`api/routes/twin.py`/`games.py` (T-04.2-02).

`target_user_id` берётся из тела запроса — это ЛЕГИТИМНО здесь (в отличие от
identity самого actor'а): участник миниаппа сам выбирает, кого поукать/
обнять/заказать анекдот/роастнуть — ровно та же роль поля, что у
`to_user_id` в `TransferBody` (`api/routes/economy.py`) или `opponent_id` в
`CreateDuelBody` (`api/routes/duel.py`). Цель резолвится ТОЛЬКО среди
участников ЭТОГО чата (join `user_balance` по `auth.chat_id` — тот же скоуп,
что уже использует `GET /api/v1/members`) — 404 на постороннего, не
произвольный Telegram user_id.

Идемпотентность: чат-хендлер (`bot/handlers/social.py`) идёт от
`message.message_id` Telegram-апдейта; у HTTP-запроса такого поля нет, так
что `social_service.do_*` были минимально адаптированы принимать
`idem_key: str` вместо `message_id: int` (см. докстринг `social_service.py`)
— здесь это клиентский `idem_key` из тела запроса, та же идиома, что уже
устоялась в `api/routes/games.py`/`farm.py`/`gacha.py`. `do_*` не коммитит
сам (форма `social_service`/`tag_rental_service` — "транзакцию завершает
вызывающий"), поэтому роут коммитит явно СРАЗУ после успешного вызова —
ровно тот же порядок, что `bot/handlers/social.py` (commit -> затем читать
свежий баланс), а не наоборот, как в `games.py`/`farm.py`/`gacha.py`, чьи
сервисы коммитят сами.

`text is None` — повтор того же `idem_key` (списание уже применено раньше,
см. докстринг `social_service.do_poke`): отдаём `replayed: true` вместо
текста — заново сгенерировать/вспомнить исходный текст неоткуда (текст
шаблона/LLM нигде не сохраняется), это тот же осознанный пробел, что и в
чат-хендлере, который в этом случае просто ничего не отправляет.

`GET /api/v1/shop` — чистый read: текущие цены четырёх действий
(`settings.social_*_cost`) для отрисовки карточек до похода к деньгам, тот
же дух, что `donate.py` читает `settings.stars_to_juvik_rate` напрямую в
роуте.

Self-target (D-03) — `social_service.InvalidTarget` -> 400 (сервис уже
отвергает это ДО списания, роут просто маппит исключение на HTTP, тот же
паттерн, что и остальные `*Error -> 400` в этом проекте).
"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Request
from pydantic import BaseModel
from pydantic import Field
from sqlalchemy import select

from api.deps import AuthContext
from api.deps import require_membership
from bot.config import settings
from bot.services import balance_events
from bot.services import economy_service
from bot.services import social_service
from common.db.session import SessionLocal
from common.models.user import User
from common.models.user_balance import UserBalance

router = APIRouter()


class TargetBody(BaseModel):
    target_user_id: int
    idem_key: str


class JokeOrderBody(TargetBody):
    topic: str = Field(min_length=1, max_length=200)


async def _resolve_target_name(session, chat_id: int, target_user_id: int) -> str:
    """Имя цели, ограниченное участниками ЭТОГО чата (join `user_balance`) —
    тот же скоуп, что `GET /api/v1/members` (T-04.2-02: не произвольный
    Telegram user_id)."""
    stmt = (
        select(User.first_name)
        .join(UserBalance, UserBalance.user_id == User.id)
        .where(UserBalance.chat_id == chat_id, User.id == target_user_id)
    )
    name = (await session.execute(stmt)).scalar_one_or_none()
    if name is None:
        raise HTTPException(status_code=404, detail="target not found in this chat")
    return name


async def _commit_and_publish_balance(request: Request, session, auth: AuthContext) -> int:
    """Коммитит транзакцию `do_*` (который сам не коммитит), затем читает и
    публикует свежий баланс actor'а в `bal:{chat_id}` (D-02, тот же паттерн,
    что `games.py`/`farm.py`/`duel.py`)."""
    await session.commit()
    balance = await economy_service.get_balance(session, auth.chat_id, auth.user_id)
    await balance_events.publish_balance(request.app.state.redis, auth.chat_id, auth.user_id, balance)
    return balance


@router.get("/api/v1/shop")
async def get_shop(auth: AuthContext = Depends(require_membership)) -> dict:
    return {
        "costs": {
            "poke": settings.social_poke_cost,
            "hug": settings.social_hug_cost,
            "joke_order": settings.social_joke_order_cost,
            "roast": settings.social_roast_cost,
        }
    }


@router.post("/api/v1/shop/poke")
async def post_poke(
    body: TargetBody, request: Request, auth: AuthContext = Depends(require_membership)
) -> dict:
    async with SessionLocal() as session:
        target_name = await _resolve_target_name(session, auth.chat_id, body.target_user_id)
        try:
            text = await social_service.do_poke(
                session, auth.chat_id, auth.user_id, body.target_user_id, target_name, body.idem_key
            )
        except (social_service.InvalidTarget, economy_service.InsufficientFunds) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        balance = await _commit_and_publish_balance(request, session, auth)
        return {"text": text, "replayed": text is None, "user_balance_after": balance}


@router.post("/api/v1/shop/hug")
async def post_hug(
    body: TargetBody, request: Request, auth: AuthContext = Depends(require_membership)
) -> dict:
    async with SessionLocal() as session:
        target_name = await _resolve_target_name(session, auth.chat_id, body.target_user_id)
        try:
            text = await social_service.do_hug(
                session, auth.chat_id, auth.user_id, body.target_user_id, target_name, body.idem_key
            )
        except (social_service.InvalidTarget, economy_service.InsufficientFunds) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        balance = await _commit_and_publish_balance(request, session, auth)
        return {"text": text, "replayed": text is None, "user_balance_after": balance}


@router.post("/api/v1/shop/joke_order")
async def post_joke_order(
    body: JokeOrderBody, request: Request, auth: AuthContext = Depends(require_membership)
) -> dict:
    async with SessionLocal() as session:
        target_name = await _resolve_target_name(session, auth.chat_id, body.target_user_id)
        try:
            text = await social_service.do_joke_order(
                session,
                auth.chat_id,
                auth.user_id,
                body.target_user_id,
                target_name,
                body.topic,
                body.idem_key,
            )
        except (social_service.InvalidTarget, economy_service.InsufficientFunds) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        balance = await _commit_and_publish_balance(request, session, auth)
        return {"text": text, "replayed": text is None, "user_balance_after": balance}


@router.post("/api/v1/shop/roast")
async def post_roast(
    body: TargetBody, request: Request, auth: AuthContext = Depends(require_membership)
) -> dict:
    async with SessionLocal() as session:
        target_name = await _resolve_target_name(session, auth.chat_id, body.target_user_id)
        try:
            text = await social_service.do_roast(
                session, auth.chat_id, auth.user_id, body.target_user_id, target_name, body.idem_key
            )
        except (social_service.InvalidTarget, economy_service.InsufficientFunds) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        balance = await _commit_and_publish_balance(request, session, auth)
        return {"text": text, "replayed": text is None, "user_balance_after": balance}
