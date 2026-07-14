"""Backfill исторических сообщений (DATA-04, D-01/D-02).

Два уровня:
- bulk_upsert_messages(session, rows) — идемпотентный батч-upsert (100-500 строк
  на батч, ON CONFLICT DO NOTHING по (chat_id, telegram_message_id) — Pattern 3,
  T-06-04): повторный прогон не создаёт дублей и не перезаписывает уже
  существующую (в т.ч. live) строку. Также upsert'ит users (Rule 2 — автор мог
  никогда не писать сообщений живым потоком) и опционально бампает
  daily_stats/word_frequency/emoji_frequency ТОЛЬКО для реально новых строк
  (та же дисциплина идемпотентности, что в message_service/CollectorMiddleware).
- run_backfill(chat_id) — ЯДРО: поднимает Kurigram Client (личный MTProto-аккаунт,
  settings.tg_api_id/tg_api_hash), итерирует ВСЮ доступную историю чата (D-01),
  батчами льёт через bulk_upsert_messages. Переиспользуется и standalone-скриптом
  (scripts/backfill_history.py), и /backfill хендлером (asyncio.create_task).

Backfilled медиа хранит file_unique_id как стабильную метку (Pitfall 4 —
file_id из userbot-сессии не воспроизводим через Bot API); source="backfill".
Без retry-петли вокруг flood-wait — Kurigram обрабатывает его внутренне
(Security Domain, T-06-02).
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from datetime import timezone
from typing import Any
from zoneinfo import ZoneInfo

from pyrogram import Client
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import settings
from bot.services import frequency_service
from common.db.session import SessionLocal
from common.models.daily_stat import DailyStat
from common.models.message import Message as MessageModel
from common.models.user import User

logger = logging.getLogger(__name__)

MSK = ZoneInfo("Europe/Moscow")

# Батч-коммиты 100-500 строк (защита от DoS на пул соединений, T-06-02).
_BATCH_SIZE = 200


@dataclass(frozen=True)
class _MediaInfo:
    file_id: str | None
    file_unique_id: str | None
    mime_type: str | None
    file_size: int | None


def _chunk(rows: list[dict[str, Any]], size: int) -> Iterable[list[dict[str, Any]]]:
    for i in range(0, len(rows), size):
        yield rows[i : i + size]


async def _upsert_users(session: AsyncSession, batch: list[dict[str, Any]]) -> None:
    """Rule 2: upsert пользователей, встреченных в батче (автор мог никогда не
    писать сообщений живым потоком, только в backfilled истории). ON CONFLICT
    DO NOTHING — не затираем уже известные (более свежие, live) данные."""
    users_by_id: dict[int, dict[str, Any]] = {}
    for row in batch:
        user_id = row.get("user_id")
        if user_id is None:
            continue
        users_by_id[user_id] = {
            "id": user_id,
            "username": row.get("username"),
            "first_name": row.get("first_name") or "",
        }
    if not users_by_id:
        return
    stmt = pg_insert(User).values(list(users_by_id.values()))
    stmt = stmt.on_conflict_do_nothing(index_elements=["id"])
    await session.execute(stmt)


def _to_naive_utc(value: datetime | None) -> datetime | None:
    """messages.created_at — TIMESTAMP WITHOUT TIME ZONE (как и server_default
    func.now() для live-строк). Kurigram/pyrogram отдаёт tz-aware datetime —
    конвертируем в UTC и снимаем tzinfo, иначе asyncpg падает с
    'can't subtract offset-naive and offset-aware datetimes'."""
    if value is None:
        return None
    if value.tzinfo is not None:
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def _message_values(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "telegram_message_id": row["telegram_message_id"],
        "chat_id": row["chat_id"],
        "user_id": row.get("user_id"),
        "text": row.get("text"),
        "reply_to_telegram_message_id": row.get("reply_to_telegram_message_id"),
        "message_thread_id": row.get("message_thread_id"),
        "content_type": row.get("content_type", "text"),
        "caption": row.get("caption"),
        "media_file_id": row.get("media_file_id"),
        "media_file_unique_id": row.get("media_file_unique_id"),
        "media_mime_type": row.get("media_mime_type"),
        "media_file_size": row.get("media_file_size"),
        "is_forwarded": row.get("is_forwarded", False),
        "source": "backfill",
        "created_at": _to_naive_utc(row.get("created_at")),
    }


async def _bump_stats_for_new_row(
    session: AsyncSession, chat_id: int, user_id: int, row: dict[str, Any]
) -> None:
    """Консистентная статистика для backfilled сообщений — переиспользует
    daily_stats-инкремент (план 02) и частотные словари (план 04). Вызывается
    только для строк, реально вставленных этим прогоном (идемпотентность)."""
    created_at: datetime | None = row.get("created_at")
    stat_date = (created_at or datetime.now(tz=MSK)).astimezone(MSK).date()

    daily_stat_stmt = pg_insert(DailyStat).values(
        chat_id=chat_id,
        user_id=user_id,
        stat_date=stat_date,
        message_count=1,
    )
    daily_stat_stmt = daily_stat_stmt.on_conflict_do_update(
        index_elements=["chat_id", "user_id", "stat_date"],
        set_={"message_count": DailyStat.message_count + daily_stat_stmt.excluded.message_count},
    )
    await session.execute(daily_stat_stmt)

    source_text = row.get("text") or row.get("caption")
    words = frequency_service.extract_words(source_text)
    emojis = frequency_service.extract_emojis(source_text)
    await frequency_service.bump_word_frequency(session, chat_id, user_id, words)
    await frequency_service.bump_emoji_frequency(session, chat_id, user_id, emojis)


async def bulk_upsert_messages(session: AsyncSession, rows: list[dict[str, Any]]) -> int:
    """Батч-идемпотентно вставляет backfilled сообщения.

    Каждый row-dict: telegram_message_id, chat_id, user_id, username, first_name,
    text, reply_to_telegram_message_id, message_thread_id, content_type, caption,
    media_file_id, media_file_unique_id, media_mime_type, media_file_size,
    is_forwarded, created_at (datetime историчного сообщения, timezone-aware).
    source="backfill" проставляется автоматически.

    Идемпотентно: ON CONFLICT (chat_id, telegram_message_id) DO NOTHING —
    повторный прогон не создаёт дублей и не перезаписывает уже существующую
    (в т.ч. live) строку (T-06-04). commit — после каждого батча (100-500 строк).

    Возвращает число реально вставленных новых строк (0 при полном повторе).
    """
    if not rows:
        return 0

    total_inserted = 0
    for batch in _chunk(rows, _BATCH_SIZE):
        await _upsert_users(session, batch)

        stmt = pg_insert(MessageModel).values([_message_values(r) for r in batch])
        stmt = stmt.on_conflict_do_nothing(index_elements=["chat_id", "telegram_message_id"])
        stmt = stmt.returning(MessageModel.chat_id, MessageModel.telegram_message_id)
        inserted_keys = {(r.chat_id, r.telegram_message_id) for r in (await session.execute(stmt)).all()}
        total_inserted += len(inserted_keys)

        if inserted_keys:
            by_key = {(r["chat_id"], r["telegram_message_id"]): r for r in batch}
            for key in inserted_keys:
                row = by_key[key]
                user_id = row.get("user_id")
                if user_id is None:
                    continue
                await _bump_stats_for_new_row(session, key[0], user_id, row)

        await session.commit()

    return total_inserted


def _extract_pyrogram_media(message: Any) -> _MediaInfo:
    """Извлекает file_id/file_unique_id/mime_type/file_size из pyrogram-сообщения
    по content_type (Pitfall 6 — все типы медиа, не только photo/sticker). В
    отличие от aiogram, message.photo в pyrogram — одиночный объект, не список."""
    if message.photo:
        p = message.photo
        return _MediaInfo(p.file_id, p.file_unique_id, None, p.file_size)
    if message.video:
        v = message.video
        return _MediaInfo(v.file_id, v.file_unique_id, v.mime_type, v.file_size)
    if message.voice:
        v = message.voice
        return _MediaInfo(v.file_id, v.file_unique_id, v.mime_type, v.file_size)
    if message.audio:
        a = message.audio
        return _MediaInfo(a.file_id, a.file_unique_id, a.mime_type, a.file_size)
    if message.document:
        d = message.document
        return _MediaInfo(d.file_id, d.file_unique_id, d.mime_type, d.file_size)
    if message.sticker:
        s = message.sticker
        return _MediaInfo(s.file_id, s.file_unique_id, None, s.file_size)
    return _MediaInfo(None, None, None, None)


def _pyrogram_content_type(message: Any) -> str:
    if message.photo:
        return "photo"
    if message.video:
        return "video"
    if message.voice:
        return "voice"
    if message.audio:
        return "audio"
    if message.document:
        return "document"
    if message.sticker:
        return "sticker"
    return "text"


def _pyrogram_message_to_row(message: Any, chat_id: int) -> dict[str, Any] | None:
    """Конвертирует pyrogram.types.Message в row-dict для bulk_upsert_messages.

    Пропускает служебные сообщения и сообщения без автора (Pitfall 5 — тот же
    None-check, что в CollectorMiddleware): системные события (join/leave и т.п.)
    и сообщения ботов в backfill не нужны.
    """
    if getattr(message, "service", None) or getattr(message, "empty", False):
        return None
    if message.from_user is None or message.from_user.is_bot:
        return None

    media = _extract_pyrogram_media(message)

    return {
        "telegram_message_id": message.id,
        "chat_id": chat_id,
        "user_id": message.from_user.id,
        "username": message.from_user.username,
        "first_name": message.from_user.first_name or "",
        "text": message.text,
        "reply_to_telegram_message_id": message.reply_to_message_id,
        "message_thread_id": getattr(message, "message_thread_id", None),
        "content_type": _pyrogram_content_type(message),
        "caption": message.caption,
        "media_file_id": media.file_id,
        "media_file_unique_id": media.file_unique_id,
        "media_mime_type": media.mime_type,
        "media_file_size": media.file_size,
        "is_forwarded": message.forward_origin is not None,
        "created_at": message.date,
    }


async def run_backfill(chat_id: int) -> int:
    """Ядро backfill (D-01): поднимает Kurigram Client (личный MTProto-аккаунт),
    итерирует ВСЮ доступную историю чата, идемпотентно льёт батчами через
    bulk_upsert_messages. Вызывается standalone-скриптом
    (scripts/backfill_history.py) и /backfill хендлером (asyncio.create_task,
    in-process, без subprocess).

    При отсутствии TG_API_ID/TG_API_HASH бросает понятную ошибку. Kurigram сам
    ждёт flood-wait внутренне — без retry-петли вокруг него (T-06-02).

    Возвращает общее число реально вставленных новых сообщений.
    """
    if settings.tg_api_id is None or settings.tg_api_hash is None:
        raise RuntimeError(
            "Backfill требует TG_API_ID и TG_API_HASH личного аккаунта "
            "(получить на https://my.telegram.org -> API development tools). "
            "Добавьте их в .env и перезапустите."
        )

    logger.info("run_backfill: старт для chat_id=%s", chat_id)

    total_inserted = 0
    batch: list[dict[str, Any]] = []

    async with Client(
        "yuvi_backfill_session",
        api_id=settings.tg_api_id,
        api_hash=settings.tg_api_hash,
    ) as app:
        async for message in app.get_chat_history(chat_id):
            row = _pyrogram_message_to_row(message, chat_id)
            if row is None:
                continue
            batch.append(row)
            if len(batch) >= _BATCH_SIZE:
                async with SessionLocal() as session:
                    total_inserted += await bulk_upsert_messages(session, batch)
                batch = []

        if batch:
            async with SessionLocal() as session:
                total_inserted += await bulk_upsert_messages(session, batch)

    logger.info(
        "run_backfill: завершён для chat_id=%s, новых сообщений: %s", chat_id, total_inserted
    )
    return total_inserted
