"""GET /api/v1/victim/today, /api/v1/awards/today, /api/v1/mood — thin routes
over victim_service/awards_service/mood_service so the "Жертва дня"/"Итоги
дня"/mood-toxicity chat commands (bot/handlers/victim.py, awards.py,
mood.py) also have a Mini App screen (hub-parity request).

Both victim_service.run_victim and awards_service.run_awards are the SAME
idempotent-per-MSK-day functions the chat commands call — hitting them from
the Mini App is safe (repeat calls the same day return the already-computed
result, no double payout) and requires no new backend logic.

Scope cut: bot/handlers/victim.py additionally grants a real Telegram
custom_title via tag_service.grant_title(bot, ...), which needs a live
aiogram Bot instance — this FastAPI process has none (it only ever talks to
Telegram via api/telegram_client.py's plain httpx client, D-01). The daily
10:00 MSK autopost (victim_service.register_daily_autopost) already grants
that title once per day regardless of who/what triggers run_victim first, so
in practice this route essentially never needs to grant it itself. This
route intentionally skips the title grant rather than stand up a second Bot
client just for that edge case.
"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi import Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.deps import AuthContext
from api.deps import require_membership
from bot.services import awards_service
from bot.services import mood_service
from bot.services import victim_service
from common.db.session import SessionLocal
from common.models.user import User

router = APIRouter()


async def _display_name(session: AsyncSession, user_id: int) -> str:
    name = (
        await session.execute(select(User.first_name).where(User.id == user_id))
    ).scalar_one_or_none()
    return name or str(user_id)


@router.get("/api/v1/victim/today")
async def get_victim_today(auth: AuthContext = Depends(require_membership)) -> dict:
    async with SessionLocal() as session:
        result = await victim_service.run_victim(session, auth.chat_id)
        if result["winner"] is None:
            return {"winner_name": None, "prize": 0, "expires_at": None, "is_new": False}
        name = await _display_name(session, result["winner"])
        return {
            "winner_name": name,
            "prize": result["prize"],
            "expires_at": result["expires_at"],
            "is_new": result["is_new"],
        }


@router.get("/api/v1/awards/today")
async def get_awards_today(auth: AuthContext = Depends(require_membership)) -> dict:
    async with SessionLocal() as session:
        result = await awards_service.run_awards(session, auth.chat_id)
        nominations = []
        for nom in result["nominations"]:
            winner_name = None
            if nom["winner_user_id"] is not None:
                winner_name = await _display_name(session, nom["winner_user_id"])
            nominations.append(
                {
                    "key": nom["key"],
                    "label": nom["label"],
                    "prize": nom["prize"],
                    "winner_name": winner_name,
                    "paid": nom["paid"],
                }
            )
        return {
            "day_msk": result["day_msk"],
            "nominations": nominations,
            "steam_game": result["steam_game"],
        }


@router.get("/api/v1/mood")
async def get_mood(days: int | None = None, auth: AuthContext = Depends(require_membership)) -> dict:
    async with SessionLocal() as session:
        mood = await mood_service.get_chat_mood(session, auth.chat_id, days)
        toxicity = await mood_service.get_chat_toxicity(session, auth.chat_id, days)
        return {"mood": mood, "toxicity": toxicity}
