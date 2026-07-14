"""Фоновый воркер эмбеддингов сообщений (для гибридного поиска /ask, /topics).

run_once(session) — SELECT сообщений без строки в message_embeddings (LEFT
JOIN ... WHERE message_id IS NULL) с непустым текстом, LIMIT 200; шлёт тексты
в nlp_client.embed_batch, апсертит результат в message_embeddings через
ON CONFLICT DO NOTHING по message_id (T-02-12 — идемпотентно, повторный тик
не пересчитывает уже посчитанные эмбеддинги).

register(scheduler, bot) — регистрирует run_once как interval-job (45с) в
переданном APScheduler, той же дисциплины broad-except + logger.exception,
что nlp_classifier (T-02-13 — тик не роняет планировщик).

Единственный писатель эмбеддингов в БД — бот; nlp остаётся stateless
(RESEARCH.md Anti-Patterns).
"""

from __future__ import annotations

import logging

from aiogram import Bot
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from bot.services import nlp_client
from common.db.session import SessionLocal
from common.models.message import Message
from common.models.message_embedding import MessageEmbedding

logger = logging.getLogger(__name__)

_BATCH_SIZE = 200
_JOB_ID = "embed_pending"


async def run_once(session: AsyncSession) -> int:
    """Один тик эмбеддинга. Возвращает число обработанных строк (0, если нечего делать)."""
    stmt = (
        select(Message.id, Message.chat_id, Message.text)
        .outerjoin(MessageEmbedding, MessageEmbedding.message_id == Message.id)
        .where(MessageEmbedding.message_id.is_(None), Message.text.is_not(None))
        .limit(_BATCH_SIZE)
    )
    rows = (await session.execute(stmt)).all()
    if not rows:
        return 0

    texts = [row.text for row in rows]
    embeddings = await nlp_client.embed_batch(texts)

    values = [
        {"message_id": row.id, "chat_id": row.chat_id, "embedding": embedding}
        for row, embedding in zip(rows, embeddings)
    ]
    insert_stmt = pg_insert(MessageEmbedding).values(values)
    insert_stmt = insert_stmt.on_conflict_do_nothing(index_elements=["message_id"])
    await session.execute(insert_stmt)

    await session.commit()
    return len(rows)


def register(scheduler: AsyncIOScheduler, bot: Bot) -> None:
    """Регистрирует фоновый эмбеддинг-воркер как interval-job (45с)."""

    async def _job() -> None:
        async with SessionLocal() as session:
            try:
                count = await run_once(session)
                if count:
                    logger.info("embed_worker: посчитано эмбеддингов %s", count)
            except Exception:  # noqa: BLE001 - job обязан пережить любую ошибку и не уронить планировщик
                logger.exception("embed_worker: тик эмбеддинга упал")

    scheduler.add_job(
        _job,
        "interval",
        seconds=45,
        coalesce=True,
        max_instances=1,
        misfire_grace_time=45,
        id=_JOB_ID,
        replace_existing=True,
    )
