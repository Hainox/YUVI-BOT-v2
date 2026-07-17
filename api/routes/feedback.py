"""POST /api/v1/feedback (member) + GET/PATCH /api/v1/admin/feedback (admin)
— CASINO-03, D-04/D-05.

`author` (user_id/chat_id) берётся ИСКЛЮЧИТЕЛЬНО из `AuthContext`
(`require_membership`), никогда из тела запроса (IDOR, T-04.3-01) — та же
дисциплина, что `economy.py::post_transfer`/`markets.py::post_bet`:
`FeedbackBody` намеренно НЕ содержит поле автора, любое лишнее поле в
JSON-теле (например поддельный `user_id`) Pydantic молча игнорирует.

Admin-роуты гейтятся `require_admin` (живой `getChatMember`, НЕ
`BOT_ADMIN_IDS`) — T-04.3-02.
"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from pydantic import BaseModel
from pydantic import Field

from api.deps import AuthContext
from api.deps import require_admin
from api.deps import require_membership
from bot.services import feedback_service
from common.db.session import SessionLocal

router = APIRouter()


class FeedbackBody(BaseModel):
    category: str
    text: str = Field(min_length=1, max_length=2000)


class ResolveBody(BaseModel):
    resolved: bool


@router.post("/api/v1/feedback")
async def post_feedback(
    body: FeedbackBody, auth: AuthContext = Depends(require_membership)
) -> dict:
    async with SessionLocal() as session:
        try:
            await feedback_service.submit(
                session, auth.chat_id, auth.user_id, body.category, body.text
            )
        except feedback_service.InvalidCategory as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        await session.commit()
    return {"status": "ok"}


@router.get("/api/v1/admin/feedback")
async def get_admin_feedback(auth: AuthContext = Depends(require_admin)) -> list[dict]:
    async with SessionLocal() as session:
        return await feedback_service.list_feedback(session, auth.chat_id)


@router.patch("/api/v1/admin/feedback/{feedback_id}")
async def patch_admin_feedback(
    feedback_id: int, body: ResolveBody, auth: AuthContext = Depends(require_admin)
) -> dict:
    async with SessionLocal() as session:
        toggled = await feedback_service.set_resolved(
            session, auth.chat_id, feedback_id, body.resolved
        )
        if not toggled:
            raise HTTPException(status_code=404, detail="feedback not found")
        await session.commit()
    return {"status": "ok"}
