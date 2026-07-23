"""GET/POST /api/v1/twin/* — тонкие роуты над `twin_service` для onboarding-
промпта в miniapp (запрошено пользователем 2026-07-23): при следующем
открытии miniapp участник, для которого ещё нет строки `twin_opt_ins`,
должен один раз увидеть вопрос "подключить AI-двойника?" — эти роуты читают
текущий статус и пишут его ответ. `user_id`/`chat_id` — ТОЛЬКО из
`AuthContext` (тот же IDOR-контракт, что у остальных роутов, T-04.2-02).

Новое значение статуса `"declined"` (`set_opt_in` и так принимал произвольную
строку, `twin_service.py` уже комментирует только 'active'/'paused' — миграция
не нужна, колонка `status` не имеет CHECK-constraint) ведёт себя как
`None`/`"paused"` для `_check_consent` (не 'active' -> нет доступа), но,
в отличие от `None`, отличает "явно отказался" от "ещё не спрашивали" — на
этом и построен `asked` ниже.
"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi import Depends

from api.deps import AuthContext
from api.deps import require_membership
from bot.services import twin_service
from common.db.session import SessionLocal

router = APIRouter()


@router.get("/api/v1/twin/status")
async def get_twin_status(auth: AuthContext = Depends(require_membership)) -> dict:
    async with SessionLocal() as session:
        status = await twin_service.get_status(session, auth.chat_id, auth.user_id)
    return {"status": status, "asked": status is not None}


@router.post("/api/v1/twin/optin")
async def post_twin_optin(auth: AuthContext = Depends(require_membership)) -> dict:
    async with SessionLocal() as session:
        await twin_service.set_opt_in(session, auth.chat_id, auth.user_id, "active")
        await session.commit()
    return {"status": "active"}


@router.post("/api/v1/twin/decline")
async def post_twin_decline(auth: AuthContext = Depends(require_membership)) -> dict:
    async with SessionLocal() as session:
        await twin_service.set_opt_in(session, auth.chat_id, auth.user_id, "declined")
        await session.commit()
    return {"status": "declined"}
