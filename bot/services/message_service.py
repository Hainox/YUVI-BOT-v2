"""Сервис записи сообщений: upsert пользователя, идемпотентная запись сообщения,
инкремент дневной агрегированной статистики (daily_stats), append-only история
правок (D-03).

Вызывается из CollectorMiddleware (save_message) и bot/handlers/edits.py
(save_edit). Медиа всех типов извлекается по content_type (Pitfall 6 —
не только photo/sticker), forward_origin (не устаревший forward_from).
"""

from __future__ import annotations

from dataclasses import dataclass
from zoneinfo import ZoneInfo

from aiogram.types import Message
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from common.models.daily_stat import DailyStat
from common.models.message import Message as MessageModel
from common.models.message_edit import MessageEdit
from common.models.user import User

MSK = ZoneInfo("Europe/Moscow")


@dataclass(frozen=True)
class _MediaInfo:
    file_id: str | None
    file_unique_id: str | None
    mime_type: str | None
    file_size: int | None


def _extract_media(event: Message) -> _MediaInfo:
    """Извлекает file_id/file_unique_id/mime_type/file_size по content_type
    (Pitfall 6): все типы медиа, не только photo/sticker.
    """
    if event.photo:
        # Список размеров, последний элемент — самое большое изображение.
        largest = event.photo[-1]
        return _MediaInfo(largest.file_id, largest.file_unique_id, None, largest.file_size)
    if event.video:
        v = event.video
        return _MediaInfo(v.file_id, v.file_unique_id, v.mime_type, v.file_size)
    if event.voice:
        v = event.voice
        return _MediaInfo(v.file_id, v.file_unique_id, v.mime_type, v.file_size)
    if event.audio:
        a = event.audio
        return _MediaInfo(a.file_id, a.file_unique_id, a.mime_type, a.file_size)
    if event.document:
        d = event.document
        return _MediaInfo(d.file_id, d.file_unique_id, d.mime_type, d.file_size)
    if event.sticker:
        s = event.sticker
        return _MediaInfo(s.file_id, s.file_unique_id, None, s.file_size)
    return _MediaInfo(None, None, None, None)


async def save_message(session: AsyncSession, event: Message) -> bool:
    """Пишет пользователя + сообщение (идемпотентно) + инкремент daily_stats.

    Требует, чтобы event.from_user не был None (проверяется в CollectorMiddleware
    до вызова — анонимные админы/боты сюда не попадают).

    Возвращает True, если сообщение было реально вставлено впервые, и False,
    если это повторная обработка того же (chat_id, telegram_message_id)
    (on_conflict_do_nothing пропустил дубль) — вызывающий код (CollectorMiddleware)
    должен использовать это значение, чтобы не бампать частоты слов/эмодзи
    повторно на ретрае (та же идемпотентность, что уже применена к daily_stats).
    """
    user = event.from_user
    assert user is not None

    user_stmt = pg_insert(User).values(
        id=user.id,
        username=user.username,
        first_name=user.first_name or "",
    )
    user_stmt = user_stmt.on_conflict_do_update(
        index_elements=["id"],
        set_={
            "username": user_stmt.excluded.username,
            "first_name": user_stmt.excluded.first_name,
        },
    )
    await session.execute(user_stmt)

    media = _extract_media(event)

    message_stmt = pg_insert(MessageModel).values(
        telegram_message_id=event.message_id,
        chat_id=event.chat.id,
        user_id=user.id,
        text=event.text,
        reply_to_telegram_message_id=(
            event.reply_to_message.message_id if event.reply_to_message else None
        ),
        message_thread_id=event.message_thread_id,
        content_type=event.content_type.value,
        caption=event.caption,
        media_file_id=media.file_id,
        media_file_unique_id=media.file_unique_id,
        media_mime_type=media.mime_type,
        media_file_size=media.file_size,
        # Bot API 7.0+: forward_origin, НЕ устаревший forward_from — последний
        # не заполняется, если пересылающий пользователь скрыл своё имя.
        is_forwarded=event.forward_origin is not None,
    )
    message_stmt = message_stmt.on_conflict_do_nothing(
        index_elements=["chat_id", "telegram_message_id"],
    )
    message_result = await session.execute(message_stmt)

    if message_result.rowcount == 0:
        # T-02-04: сообщение уже было записано раньше (ретрай после краша до
        # commit) — строка messages не вставилась повторно (on_conflict_do_nothing),
        # поэтому daily_stats тоже НЕ инкрементируем: иначе один и тот же message_id
        # задвоил бы счётчик при повторной обработке (полная идемпотентность записи).
        return False

    stat_date = event.date.astimezone(MSK).date()
    daily_stat_stmt = pg_insert(DailyStat).values(
        chat_id=event.chat.id,
        user_id=user.id,
        stat_date=stat_date,
        message_count=1,
    )
    daily_stat_stmt = daily_stat_stmt.on_conflict_do_update(
        index_elements=["chat_id", "user_id", "stat_date"],
        set_={"message_count": DailyStat.message_count + daily_stat_stmt.excluded.message_count},
    )
    await session.execute(daily_stat_stmt)
    return True


async def save_edit(
    session: AsyncSession, chat_id: int, telegram_message_id: int, new_text: str | None
) -> bool:
    """Append-only запись правки сообщения (D-03).

    Резолвит внутренний message по (chat_id, telegram_message_id); если найден —
    INSERT новую строку в message_edits. НИКОГДА не UPDATE messages.text —
    оригинал остаётся честным для будущей детекции мата/статистики.

    Если сообщение не найдено (например, правка на несохранённое до старта
    сбора сообщение) — пропускает запись, возвращает False.
    """
    message = (
        await session.execute(
            select(MessageModel).where(
                MessageModel.chat_id == chat_id,
                MessageModel.telegram_message_id == telegram_message_id,
            )
        )
    ).scalar_one_or_none()
    if message is None:
        return False

    await session.execute(
        pg_insert(MessageEdit).values(
            message_id=message.id,
            chat_id=chat_id,
            telegram_message_id=telegram_message_id,
            new_text=new_text,
        )
    )
    return True
