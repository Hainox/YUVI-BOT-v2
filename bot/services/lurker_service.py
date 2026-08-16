"""Ежедневный мемный автопост про «наблюдателей» чата (LURKER-01) — Дениска PC
Nation и Димочка Yuvi, легендарные для этого чата персонажи, которые якобы
день и ночь следят за перепиской через фейковые аккаунты и фиксируют всё в
деталях, но сами под своими именами в чат так и не заходят. Чисто
косметический, не-игровой автопост (нет денег/титулов/состояния — в отличие
от victim_service/awards_service) — просто ежедневная шутка, свежая каждый
раз через LLM, форма victim_service.register_daily_autopost/
tag_service.register_title_expiry: своя SessionLocal, cron, broad-except (тик
обязан пережить любую ошибку, включая сбой LLM, и не уронить планировщик —
здесь, в отличие от bot/handlers/social.py::roast_command/joke_order_command,
никто не ждёт ответа в моменте, поэтому при сбое просто пропускаем сегодняшний
пост, а не шлём пользователю сообщение об ошибке).
"""

from __future__ import annotations

import logging
from zoneinfo import ZoneInfo

from aiogram import Bot
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import settings
from bot.services import ai_client
from bot.services import settings_service
from common.db.session import SessionLocal

logger = logging.getLogger(__name__)

MSK = ZoneInfo("Europe/Moscow")

# Дословные имена мема этого чата — не выдумывать других, не подставлять
# реальные @username/user_id (T-05-01-подобный принцип: это подколка про
# формальное отсутствие в чате, а не настоящее упоминание/пуш конкретного
# Telegram-аккаунта).
_SYSTEM_PROMPT = (
    "Ты — циничный ассистент чата друзей. Сочини короткое (2-4 предложения) "
    "ироничное сообщение на русском про Дениску PC Nation и Димочку Yuvi — "
    "легендарных для этого чата персонажей, которые якобы круглосуточно "
    "следят за перепиской через фейковые аккаунты, фиксируют каждую мелочь "
    "и знают всё про всех, но почему-то так и не заходят в чат под своими "
    "именами. Тон — едкий, саркастичный, с подколкой. НЕ используй нытьё, "
    "жалобные фразы вроде «нам грустно»/«как жаль» и вообще любую грусть — "
    "только чистая ирония и подъёб. Каждый раз формулируй по-новому, не "
    "повторяй одну и ту же шутку слово в слово."
)


async def build_daily_message(session: AsyncSession, chat_id: int) -> str:
    """Генерирует сегодняшний текст (свежий каждый раз, без кеша/сохранения —
    сообщение не несёт состояния, которое нужно было бы переиграть при
    повторном тике). Хэштег #мем дописывается ЗДЕСЬ детерминированно, а не
    доверяется модели (D-02/Pitfall 8: не полагаться на то, что модель
    не забудет про инструкцию)."""
    model = await settings_service.get_active_model(session, chat_id, default=settings.ai_structured_model)
    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": "Напиши сегодняшнее сообщение."},
    ]
    text = (
        await ai_client.complete_with_fallback(
            messages, primary_model=model, max_tokens=settings.ai_max_output_tokens
        )
    ).strip()
    if not text:
        return ""
    return f"{text}\n\n#мем"


# --- register_daily_roast (APScheduler, форма victim_service.register_daily_autopost) --

_JOB_ID = "lurker_daily_roast"


def register_daily_roast(scheduler: AsyncIOScheduler, bot: Bot) -> None:
    """Регистрирует автопост 12:00 МСК (cron) в settings.chat_id.
    coalesce+max_instances=1 — пропущенные срабатывания не постят несколько
    раз подряд при догоняющем запуске."""

    async def _job() -> None:
        async with SessionLocal() as session:
            try:
                text = await build_daily_message(session, settings.chat_id)
                if not text:
                    return
                await bot.send_message(settings.chat_id, text)
            except Exception:  # noqa: BLE001 - тик обязан пережить любую ошибку
                logger.exception("lurker_daily_roast: тик упал")

    scheduler.add_job(
        _job,
        "cron",
        hour=12,
        minute=0,
        timezone=MSK,
        coalesce=True,
        max_instances=1,
        misfire_grace_time=3600,
        id=_JOB_ID,
        replace_existing=True,
    )
