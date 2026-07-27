"""GET/POST /api/v1/ai/* — thin routes over ask_service/card_service/
digest_service/summary_service/topics_service/phrase_service/joke_service so
the AI/NLP chat commands (bot/handlers/ai_ask.py, ai_card.py, ai_digest.py,
ai_summary.py, ai_topics.py) also have a Mini App screen (hub-parity
request).

Streaming (summary_service.stream_summary) is collected server-side into one
string rather than streamed to the Mini App: the bot's stream_into_message
exists to edit a single Telegram message in place as chunks arrive (Telegram
API constraint), which doesn't apply here — the miniapp waits for the request
like every other apiFetch call, same simplification twin_service.build_twin_reply
and card_service.build_portrait already make for their own ai_client.stream()
calls (join the chunks, return one string).

Every route wraps its service call in a broad except, matching the "handler
must tell the user something went wrong, not fail silently" convention used
by every one of these commands' chat handlers (ai_card.py, ai_digest.py,
ai_topics.py, ask_service's own caller) — surfaced here as a 502 with a
friendly detail string instead of a chat reply.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.deps import AuthContext
from api.deps import require_membership
from bot.config import settings
from bot.services import ask_service
from bot.services import card_service
from bot.services import digest_service
from bot.services import joke_service
from bot.services import phrase_service
from bot.services import summary_service
from bot.services import topics_service
from common.db.session import SessionLocal
from common.models.user import User

router = APIRouter()
logger = logging.getLogger(__name__)


async def _display_name(session: AsyncSession, user_id: int) -> str:
    name = (
        await session.execute(select(User.first_name).where(User.id == user_id))
    ).scalar_one_or_none()
    return name or str(user_id)


class AskBody(BaseModel):
    question: str


@router.post("/api/v1/ai/ask")
async def post_ai_ask(body: AskBody, auth: AuthContext = Depends(require_membership)) -> dict:
    question = body.question.strip()[: settings.ai_ask_max_query_chars]
    if not question:
        raise HTTPException(status_code=400, detail="question is required")
    async with SessionLocal() as session:
        try:
            answer = await ask_service.answer(session, auth.chat_id, question)
        except Exception:  # noqa: BLE001 - см. модульный докстринг
            logger.exception("ask_service.answer упал для chat_id=%s", auth.chat_id)
            raise HTTPException(status_code=502, detail="Не удалось найти ответ — попробуйте позже.")
    return {"answer": answer}


@router.get("/api/v1/ai/card")
async def get_ai_card(
    target_user_id: int | None = None, auth: AuthContext = Depends(require_membership)
) -> dict:
    async with SessionLocal() as session:
        user_id = target_user_id if target_user_id is not None else auth.user_id
        display_name = await _display_name(session, user_id)
        try:
            card = await card_service.build_card(session, auth.chat_id, user_id, display_name)
        except Exception:  # noqa: BLE001 - см. модульный докстринг
            logger.exception(
                "build_card упал для chat_id=%s user_id=%s", auth.chat_id, user_id
            )
            raise HTTPException(status_code=502, detail="Не удалось собрать карточку — попробуйте позже.")
    return {"display_name": display_name, **card}


@router.get("/api/v1/ai/digest")
async def get_ai_digest(days: int = 1, auth: AuthContext = Depends(require_membership)) -> dict:
    async with SessionLocal() as session:
        try:
            text = await digest_service.build_manual_digest(session, auth.chat_id, days)
        except Exception:  # noqa: BLE001 - см. модульный докстринг
            logger.exception("build_manual_digest упал для chat_id=%s", auth.chat_id)
            raise HTTPException(status_code=502, detail="Не удалось собрать дайджест — попробуйте позже.")
    return {"text": text}


@router.get("/api/v1/ai/summary")
async def get_ai_summary(n: int = 100, auth: AuthContext = Depends(require_membership)) -> dict:
    async with SessionLocal() as session:
        try:
            parts = [delta async for delta in summary_service.stream_summary(session, auth.chat_id, n, None)]
        except Exception:  # noqa: BLE001 - см. модульный докстринг
            logger.exception("stream_summary упал для chat_id=%s", auth.chat_id)
            raise HTTPException(status_code=502, detail="Не удалось получить пересказ — попробуйте позже.")
    return {"text": "".join(parts)}


class SummaryCustomBody(BaseModel):
    n: int
    focus: str


@router.post("/api/v1/ai/summary_custom")
async def post_ai_summary_custom(
    body: SummaryCustomBody, auth: AuthContext = Depends(require_membership)
) -> dict:
    focus = body.focus.strip()[: settings.ai_max_custom_prompt_chars]
    if not focus:
        raise HTTPException(status_code=400, detail="focus is required")
    async with SessionLocal() as session:
        try:
            parts = [
                delta
                async for delta in summary_service.stream_summary(session, auth.chat_id, body.n, focus)
            ]
        except Exception:  # noqa: BLE001 - см. модульный докстринг
            logger.exception("stream_summary (custom) упал для chat_id=%s", auth.chat_id)
            raise HTTPException(status_code=502, detail="Не удалось получить пересказ — попробуйте позже.")
    return {"text": "".join(parts)}


@router.get("/api/v1/ai/topics")
async def get_ai_topics(auth: AuthContext = Depends(require_membership)) -> dict:
    async with SessionLocal() as session:
        try:
            text = await topics_service.build_topics(session, auth.chat_id)
        except Exception:  # noqa: BLE001 - см. модульный докстринг
            logger.exception("build_topics упал для chat_id=%s", auth.chat_id)
            raise HTTPException(status_code=502, detail="Не удалось выделить темы — попробуйте позже.")
    return {"text": text}


@router.get("/api/v1/ai/phrase")
async def get_ai_phrase(auth: AuthContext = Depends(require_membership)) -> dict:
    async with SessionLocal() as session:
        try:
            text = await phrase_service.build_phrase(session, auth.chat_id)
        except Exception:  # noqa: BLE001 - см. модульный докстринг
            logger.exception("build_phrase упал для chat_id=%s", auth.chat_id)
            raise HTTPException(status_code=502, detail="Не удалось выбрать фразу дня — попробуйте позже.")
    return {"text": text}


@router.get("/api/v1/ai/joke")
async def get_ai_joke(auth: AuthContext = Depends(require_membership)) -> dict:
    async with SessionLocal() as session:
        try:
            text = await joke_service.build_joke(session, auth.chat_id)
        except Exception:  # noqa: BLE001 - см. модульный докстринг
            logger.exception("build_joke упал для chat_id=%s", auth.chat_id)
            raise HTTPException(status_code=502, detail="Не удалось сочинить анекдот — попробуйте позже.")
    return {"text": text}
