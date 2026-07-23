"""GET/POST /api/v1/tags/* — тонкие роуты над `tag_rental_service` (TAG-02:
рынок аренды Telegram custom_title). `user_id`/`chat_id` — ТОЛЬКО из
`AuthContext` (`require_membership`), тот же IDOR-контракт, что и остальные
роуты. Действуют ТОЛЬКО на вызывающего (V4, T-05-04) — та же дисциплина, что
`bot/handlers/tags.py`: этот модуль не принимает чужой user_id вовсе, в
отличие от `/transfer`/`/shop/*`.

`bot: TagBotLike` (`tag_rental_service.rent_title`/`cancel_rental` зовут Bot
API напрямую внутри) — процесс `api` не держит aiogram `Bot`-инстанс
(`api/telegram_client.py`/`api/duel_mute.py`), поэтому сюда передаётся
`api.tag_apply.ApiTagBot` — raw-httpx реализация того же узкого
`TagBotLike`-протокола, что аренда и так уже принимает (см. докстринги
`tag_service.py`/`api/tag_apply.py`).

Идемпотентность аренды: `ref_id` строится ЗДЕСЬ из клиентского `idem_key`
(`f"tag_rent:{chat_id}:{idem_key}"`) — та же форма, что чат-хендлер строит
из `message.message_id` (`bot/handlers/tags.py`), просто с HTTP-стороны нет
message_id, поэтому клиент присылает свой ключ (та же идиома, что `ref_id`/
`idem_key` в `games.py`/`farm.py`/`duel.py`). `tag_rental_service.rent_title`
не коммитит сам — роут коммитит явно после успеха, тот же порядок, что
`bot/handlers/tags.py` (commit -> ответ).

`GET /api/v1/tags` — чистый read: текущая цена/лимиты (`settings.tag_rent_*`,
`settings.title_max`) + текущая аренда вызывающего, если есть
(`tag_rental_service.get_active_rental`) — miniapp показывает её вместо
формы новой аренды.

Исключения: `TagRentalError`/`tag_service.TagError` (невалидные days/title)
-> 400; `economy_service.InsufficientFunds` -> 400.
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
from api.tag_apply import ApiTagBot
from bot.config import settings
from bot.services import balance_events
from bot.services import economy_service
from bot.services import tag_rental_service
from bot.services import tag_service
from bot.utils.time import to_utc_iso
from common.db.session import SessionLocal
from common.models.active_title import ActiveTitle

router = APIRouter()


class RentBody(BaseModel):
    title: str = Field(min_length=1, max_length=settings.title_max)
    days: int
    idem_key: str


def _serialize_rental(row: ActiveTitle) -> dict:
    return {
        "title": row.title,
        "status": row.status,
        "price_paid": row.price_paid,
        "expires_at": to_utc_iso(row.expires_at) if row.expires_at else None,
    }


def _allowed_days() -> list[int]:
    return sorted(int(chunk) for chunk in settings.tag_rent_allowed_days.split(",") if chunk.strip())


@router.get("/api/v1/tags")
async def get_tags(auth: AuthContext = Depends(require_membership)) -> dict:
    async with SessionLocal() as session:
        active = await tag_rental_service.get_active_rental(session, auth.chat_id, auth.user_id)
    return {
        "pricing": {
            "per_day": settings.tag_rent_per_day,
            "allowed_days": _allowed_days(),
            "title_max": settings.title_max,
        },
        "active": _serialize_rental(active) if active is not None else None,
    }


@router.post("/api/v1/tags/rent")
async def post_rent(
    body: RentBody, request: Request, auth: AuthContext = Depends(require_membership)
) -> dict:
    async with SessionLocal() as session:
        ref_id = f"tag_rent:{auth.chat_id}:{body.idem_key}"
        bot = ApiTagBot(request.app.state.http_client, settings.bot_token)
        try:
            row = await tag_rental_service.rent_title(
                session, auth.chat_id, auth.user_id, body.title, body.days, ref_id, bot
            )
        except (tag_rental_service.TagRentalError, tag_service.TagError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except economy_service.InsufficientFunds as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        await session.commit()
        balance = await economy_service.get_balance(session, auth.chat_id, auth.user_id)
        await balance_events.publish_balance(request.app.state.redis, auth.chat_id, auth.user_id, balance)

        result = _serialize_rental(row)
        result["user_balance_after"] = balance
        return result


@router.post("/api/v1/tags/cancel")
async def post_cancel(request: Request, auth: AuthContext = Depends(require_membership)) -> dict:
    async with SessionLocal() as session:
        bot = ApiTagBot(request.app.state.http_client, settings.bot_token)
        cancelled = await tag_rental_service.cancel_rental(session, auth.chat_id, auth.user_id, bot)
        await session.commit()
        return {"cancelled": cancelled}
