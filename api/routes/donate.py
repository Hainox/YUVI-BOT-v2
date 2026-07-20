"""POST /api/v1/donate — второй UI-вход доната из Mini App (STARS-01, D-10).

Тот же backend-флоу, что бот-команда `/donate <N>` (`bot/handlers/donate.py`):
raw HTTP `sendInvoice` в ТОТ ЖЕ групповой чат — `api`-процесс не держит
aiogram `Bot`-инстанс (паттерн `api/telegram_client.py::get_chat_member_status`
/ `api/duel_mute.py`). Инвойс появляется в чате; оплата и идемпотентное
начисление (по `telegram_payment_charge_id`) происходят асинхронно в
`bot/handlers/donate.py::on_successful_payment` — оба UI-входа (бот-команда и
Mini App) сходятся в ОДНОЙ точке начисления, этот роут её не дублирует и не
поллит оплату.

`chat_id`/`user_id` берутся ИСКЛЮЧИТЕЛЬНО из `AuthContext` (`require_
membership`), `DonateBody` несёт только `stars` (IDOR, T-06-17) — та же
дисциплина, что `api/routes/feedback.py::post_feedback`: любое лишнее поле
в JSON-теле (например поддельный `chat_id`/`user_id`) Pydantic молча
игнорирует.
"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Request
from pydantic import BaseModel
from pydantic import Field

from api import telegram_client
from api.deps import AuthContext
from api.deps import require_membership
from bot.config import settings

router = APIRouter()

INVOICE_TITLE = "Донат Ювикам"


class DonateBody(BaseModel):
    # D-12: строго положительное целое, минимум 1⭐, без верхнего предела —
    # НЕТ chat_id/user_id (IDOR, T-06-17).
    stars: int = Field(gt=0)


@router.post("/api/v1/donate")
async def post_donate(
    body: DonateBody, request: Request, auth: AuthContext = Depends(require_membership)
) -> dict:
    result = await telegram_client.send_invoice(
        request.app.state.http_client,
        settings.bot_token,
        auth.chat_id,
        INVOICE_TITLE,
        f"{body.stars}⭐ = {body.stars * settings.stars_to_juvik_rate} ювиков",
        f"stars_donate:{auth.user_id}",
        [{"label": "Донат", "amount": body.stars}],
    )
    if not result.get("ok"):
        raise HTTPException(
            status_code=502, detail=result.get("description", "не удалось отправить счёт")
        )
    return {"status": "ok"}
