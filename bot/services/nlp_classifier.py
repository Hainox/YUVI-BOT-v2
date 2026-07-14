"""Фоновая классификация настроения/токсичности батчами по 200 (NLP-02).

run_once(session) — ядро: SELECT messages WHERE nlp_processed_at IS NULL
LIMIT 200, шлёт непустые тексты в nlp_client.classify_batch, пишет
sentiment_label/sentiment_score/toxicity_score/nlp_processed_at обратно.
Пустой текст (медиа без подписи) помечается nlp_processed_at сразу, без
похода в nlp — чтобы такие строки не оставались NULL навсегда и не
"зацикливали" выборку на одних и тех же id (T-02-12).

register(scheduler, bot) — регистрирует run_once как interval-job (30с,
coalesce=True, max_instances=1) в переданном APScheduler. Тело job'а само
открывает AsyncSession через SessionLocal (job не имеет доступа к
middleware-сессии запроса) и ловит любое исключение broad-except +
logger.exception, чтобы падение одного тика не роняло планировщик (T-02-13).

Единственный писатель NLP-результатов в БД — бот; nlp остаётся stateless
(RESEARCH.md Anti-Patterns).
"""

from __future__ import annotations

import logging

from aiogram import Bot
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import func
from sqlalchemy import select
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from bot.services import nlp_client
from common.db.session import SessionLocal
from common.models.message import Message

logger = logging.getLogger(__name__)

_BATCH_SIZE = 200
_JOB_ID = "nlp_classify_pending"


async def run_once(session: AsyncSession) -> int:
    """Один тик классификации. Возвращает число обработанных строк (0, если нечего делать)."""
    stmt = (
        select(Message.id, Message.text)
        .where(Message.nlp_processed_at.is_(None))
        .limit(_BATCH_SIZE)
    )
    rows = (await session.execute(stmt)).all()
    if not rows:
        return 0

    empty_ids = [row.id for row in rows if not row.text]
    text_rows = [(row.id, row.text) for row in rows if row.text]

    if empty_ids:
        # Медиа без текста/пустая строка — сразу помечаем обработанным (score
        # остаётся NULL), иначе строка будет выбираться на каждом тике снова.
        await session.execute(
            update(Message).where(Message.id.in_(empty_ids)).values(nlp_processed_at=func.now())
        )

    if text_rows:
        texts = [text for _, text in text_rows]
        results = await nlp_client.classify_batch(texts)
        for (message_id, _), result in zip(text_rows, results):
            await session.execute(
                update(Message)
                .where(Message.id == message_id)
                .values(
                    sentiment_label=result.get("sentiment_label"),
                    sentiment_score=result.get("sentiment_score"),
                    toxicity_score=result.get("toxicity_score"),
                    nlp_processed_at=func.now(),
                )
            )

    await session.commit()
    return len(rows)


def register(scheduler: AsyncIOScheduler, bot: Bot) -> None:
    """Регистрирует фоновую классификацию как interval-job (30с)."""

    async def _job() -> None:
        async with SessionLocal() as session:
            try:
                count = await run_once(session)
                if count:
                    logger.info("nlp_classifier: обработано %s сообщений", count)
            except Exception:  # noqa: BLE001 - job обязан пережить любую ошибку и не уронить планировщик
                logger.exception("nlp_classifier: тик классификации упал")

    scheduler.add_job(
        _job,
        "interval",
        seconds=30,
        coalesce=True,
        max_instances=1,
        misfire_grace_time=30,
        id=_JOB_ID,
        replace_existing=True,
    )
