"""Автодайджест дня + ручной /digest N (AI-02, D-01/D-02/D-03/D-12).

build_digest(session, chat_id) — за СЕГОДНЯШНИЙ день (Europe/Moscow): если
активность чата ниже settings.digest_min_messages — возвращает None БЕЗ
единого обращения к LLM (D-03/D-12, T-02-21 — чат не спамится пустым/скудным
автопостом, экономия платных вызовов). Иначе собирает три блока (D-02):
(1) AI-пересказ дня — та же логика, что /summary (summary_service), (2) топ
активных участников дня — готовые агрегаты Фазы 1 (stats_service), (3)
настроение/токсичность дня — заранее посчитанные NLP-метрики (mood_service,
чистый SQL, никаких LLM/NLP-вызовов).

build_manual_digest(session, chat_id, days) — та же сборка из трёх блоков для
ручного /digest N, но БЕЗ порога D-03: явный запрос участника не спамит чат.

count_day_messages читает daily_stats (несёт message_count) за ТОЧНУЮ дату,
а не COUNT(*) по messages (RESEARCH.md Anti-Patterns — та таблица растёт
неограниченно после backfill).

run_daily_digest(bot) — обёртка для cron-job'а (регистрируется планом 09 в
scheduler.setup_jobs): сама открывает AsyncSession через SessionLocal (job не
имеет доступа к middleware-сессии запроса), как nlp_classifier/embed_worker
(план 05); broad-except + logger.exception — падение дайджеста не должно
ронять планировщик (та же дисциплина, что T-02-13).
"""

from __future__ import annotations

import logging
from datetime import date
from datetime import datetime
from zoneinfo import ZoneInfo

from aiogram import Bot
from sqlalchemy import func
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import settings
from bot.services import mood_service
from bot.services import stats_service
from bot.services import summary_service
from common.db.session import SessionLocal
from common.models.daily_stat import DailyStat

logger = logging.getLogger(__name__)

MSK = ZoneInfo("Europe/Moscow")


def _today_msk() -> date:
    return datetime.now(MSK).date()


async def count_day_messages(session: AsyncSession, chat_id: int, day: date) -> int:
    """Число сообщений чата за КОНКРЕТНЫЙ день (Europe/Moscow) — прямой
    запрос к daily_stats по точной дате (не диапазон), не COUNT(*) по
    messages (RESEARCH.md Anti-Patterns)."""
    stmt = select(func.coalesce(func.sum(DailyStat.message_count), 0)).where(
        DailyStat.chat_id == chat_id,
        DailyStat.stat_date == day,
    )
    result = await session.execute(stmt)
    return int(result.scalar_one())


async def _summary_block(session: AsyncSession, chat_id: int, n: int) -> str:
    """Собирает полный текст AI-пересказа, стримя через summary_service (та
    же логика, что /summary) — здесь дайджест не редактирует сообщение
    поэлементно, а копит дельты в готовый текст блока."""
    parts = [delta async for delta in summary_service.stream_summary(session, chat_id, n, None)]
    text = "".join(parts).strip()
    return text or "Пересказ дня недоступен — недостаточно сообщений."


async def _participants_block(session: AsyncSession, chat_id: int, days: int | None, limit: int = 3) -> str:
    rows = await stats_service.get_top_participants(session, chat_id, days, limit=limit)
    if not rows:
        return "Топ участников: нет данных."
    lines = ["Топ участников дня:"]
    for i, row in enumerate(rows, start=1):
        name = row["first_name"] or row["username"] or str(row["user_id"])
        lines.append(f"{i}. {name} — {row['message_count']}")
    return "\n".join(lines)


async def _mood_block(session: AsyncSession, chat_id: int, days: int | None) -> str:
    mood = await mood_service.get_chat_mood(session, chat_id, days)
    toxicity = await mood_service.get_chat_toxicity(session, chat_id, days)
    if mood["classified_count"] == 0:
        return "Настроение/токсичность: пока недостаточно данных."

    shares = mood["label_shares"]
    lines = [
        "Настроение и токсичность дня:",
        f"Позитивных {shares['positive']:.0%} / "
        f"Нейтральных {shares['neutral']:.0%} / "
        f"Негативных {shares['negative']:.0%}",
    ]
    if toxicity["classified_count"]:
        lines.append(f"Токсичных сообщений: {toxicity['toxic_share']:.0%}")
    return "\n".join(lines)


def _compose_digest(header: str, summary_text: str, participants_text: str, mood_text: str) -> str:
    return "\n\n".join([header, summary_text, participants_text, mood_text])


async def build_digest(session: AsyncSession, chat_id: int) -> str | None:
    """Автодайджест за СЕГОДНЯШНИЙ день (Europe/Moscow).

    D-03/D-12: если активность за день < settings.digest_min_messages —
    возвращает None БЕЗ единого обращения к LLM (T-02-21) — вызывающий код
    (run_daily_digest) не должен слать сообщение в чат.
    """
    today = _today_msk()
    count = await count_day_messages(session, chat_id, today)
    if count < settings.digest_min_messages:
        return None

    summary_text = await _summary_block(session, chat_id, n=count)
    participants_text = await _participants_block(session, chat_id, days=1)
    mood_text = await _mood_block(session, chat_id, days=1)

    return _compose_digest("Дайджест дня", summary_text, participants_text, mood_text)


async def build_manual_digest(session: AsyncSession, chat_id: int, days: int = 1) -> str:
    """Ручной /digest N (D-02) — БЕЗ порога D-03/D-12: явный вызов участника,
    чат не может "заспамить сам себя" автопостом, поэтому digest_min_messages
    здесь намеренно не проверяется (порог применяется только к
    автодайджесту, build_digest)."""
    count = await stats_service.get_chat_message_count(session, chat_id, days)
    summary_text = await _summary_block(session, chat_id, n=count)
    participants_text = await _participants_block(session, chat_id, days=days)
    mood_text = await _mood_block(session, chat_id, days=days)

    header = f"Дайджест за последние {days} дн." if days else "Дайджест"
    return _compose_digest(header, summary_text, participants_text, mood_text)


async def run_daily_digest(bot: Bot) -> None:
    """Обёртка для cron-job'а (D-01, 22:00 МСК): сама открывает сессию через
    SessionLocal, шлёт результат build_digest в settings.chat_id, если он не
    None (D-03). broad-except + logger — падение дайджеста не должно ронять
    планировщик."""
    try:
        async with SessionLocal() as session:
            text = await build_digest(session, settings.chat_id)
        if text is not None:
            await bot.send_message(settings.chat_id, text)
    except Exception:  # noqa: BLE001 - job обязан пережить любую ошибку и не уронить планировщик
        logger.exception("run_daily_digest: не удалось собрать/отправить дайджест")
