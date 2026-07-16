"""GET /api/v1/me — баланс аутентифицированного пользователя (ECON-01, D-01).

Тонкая обёртка над `bot.services.economy_service.get_balance` — `user_id`/
`chat_id` берутся ТОЛЬКО из `AuthContext` (`require_membership`), никогда из
query/тела запроса (IDOR, T-04.2-01/T-04.2-02).
"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi import Depends

from api.deps import AuthContext
from api.deps import require_membership
from bot.services import economy_service
from common.db.session import SessionLocal

router = APIRouter()


@router.get("/api/v1/me")
async def get_me(auth: AuthContext = Depends(require_membership)) -> dict:
    async with SessionLocal() as session:
        balance = await economy_service.get_balance(session, auth.chat_id, auth.user_id)
    return {"balance": balance}
