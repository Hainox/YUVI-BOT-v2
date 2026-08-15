"""Интеграционный тест backfill_service.bulk_upsert_messages против живого
Postgres (фикстура `session` из tests/conftest.py — транзакция-на-тест).

Доказывает идемпотентность (T-06-04): двойной прогон одного и того же набора
backfilled строк не создаёт дублей и не перезаписывает уже существующую
(live) строку с тем же (chat_id, telegram_message_id).
"""

from __future__ import annotations

from datetime import datetime
from datetime import timezone
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import func
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from bot.constants import TELEGRAM_SERVICE_ACCOUNT_ID
from bot.services.backfill_service import _PROJECT_ROOT
from bot.services.backfill_service import _pyrogram_message_to_row
from bot.services.backfill_service import bulk_upsert_messages
from common.models.daily_stat import DailyStat
from common.models.message import Message as MessageModel


def _row(
    chat_id: int, telegram_message_id: int, user_id: int, text: str
) -> dict[str, Any]:
    return {
        "telegram_message_id": telegram_message_id,
        "chat_id": chat_id,
        "user_id": user_id,
        "username": "backfill_user",
        "first_name": "Бэкфилл",
        "text": text,
        "reply_to_telegram_message_id": None,
        "message_thread_id": None,
        "content_type": "text",
        "caption": None,
        "media_file_id": None,
        "media_file_unique_id": None,
        "media_mime_type": None,
        "media_file_size": None,
        "is_forwarded": False,
        "created_at": datetime(2025, 1, 1, 12, 0, tzinfo=timezone.utc),
    }


async def _insert_live_message(
    session, chat_id: int, telegram_message_id: int, text: str
) -> None:
    """Вставляет 'живую' строку напрямую (source остаётся default='live'),
    имитируя уже собранное CollectorMiddleware сообщение до запуска backfill."""
    stmt = pg_insert(MessageModel).values(
        chat_id=chat_id,
        telegram_message_id=telegram_message_id,
        user_id=None,
        text=text,
        content_type="text",
    )
    stmt = stmt.on_conflict_do_nothing(index_elements=["chat_id", "telegram_message_id"])
    await session.execute(stmt)
    await session.commit()


async def _message_count(session, chat_id: int) -> int:
    result = await session.execute(
        select(func.count()).select_from(MessageModel).where(MessageModel.chat_id == chat_id)
    )
    return result.scalar_one()


@pytest.mark.asyncio
async def test_bulk_upsert_messages_does_not_duplicate_on_second_run(session):
    chat_id = -100555001
    rows = [
        _row(chat_id, 5001, 900001, "первое backfilled сообщение"),
        _row(chat_id, 5002, 900001, "второе backfilled сообщение"),
        _row(chat_id, 5003, 900002, "третье backfilled сообщение"),
    ]

    first_inserted = await bulk_upsert_messages(session, rows)
    assert first_inserted == 3
    assert await _message_count(session, chat_id) == 3

    second_inserted = await bulk_upsert_messages(session, rows)
    assert second_inserted == 0
    assert await _message_count(session, chat_id) == 3


@pytest.mark.asyncio
async def test_bulk_upsert_messages_does_not_overwrite_existing_live_row(session):
    chat_id = -100555002
    telegram_message_id = 6001
    await _insert_live_message(session, chat_id, telegram_message_id, "живое сообщение")

    rows = [_row(chat_id, telegram_message_id, 900003, "backfilled-версия того же id")]
    inserted = await bulk_upsert_messages(session, rows)

    assert inserted == 0
    assert await _message_count(session, chat_id) == 1

    result = await session.execute(
        select(MessageModel).where(
            MessageModel.chat_id == chat_id,
            MessageModel.telegram_message_id == telegram_message_id,
        )
    )
    row = result.scalar_one()
    assert row.text == "живое сообщение"
    assert row.source == "live"


@pytest.mark.asyncio
async def test_bulk_upsert_messages_stores_source_backfill_and_file_unique_id(session):
    chat_id = -100555003
    row = _row(chat_id, 7001, 900004, "сообщение с медиа")
    row["content_type"] = "photo"
    row["media_file_id"] = "AgAC-some-userbot-file-id"
    row["media_file_unique_id"] = "stable-unique-id-001"

    inserted = await bulk_upsert_messages(session, [row])
    assert inserted == 1

    result = await session.execute(
        select(MessageModel).where(
            MessageModel.chat_id == chat_id,
            MessageModel.telegram_message_id == 7001,
        )
    )
    saved = result.scalar_one()
    assert saved.source == "backfill"
    assert saved.media_file_unique_id == "stable-unique-id-001"


@pytest.mark.asyncio
async def test_bulk_upsert_messages_bumps_daily_stats_only_for_new_rows(session):
    chat_id = -100555004
    user_id = 900005
    rows = [_row(chat_id, 8001, user_id, "первое"), _row(chat_id, 8002, user_id, "второе")]

    await bulk_upsert_messages(session, rows)
    await bulk_upsert_messages(session, rows)  # повтор — не должен задвоить счётчик

    result = await session.execute(
        select(DailyStat).where(DailyStat.chat_id == chat_id, DailyStat.user_id == user_id)
    )
    stat = result.scalar_one()
    assert stat.message_count == 2


def test_project_root_workdir_is_stable_regardless_of_entrypoint():
    """Регрессия: Client(workdir=...) по умолчанию у Pyrogram/Kurigram
    резолвится в Path(sys.argv[0]).parent — директорию ЗАПУЩЕННОГО файла, а не
    cwd процесса. Это означает, что bot/main.py, scripts/backfill_history.py
    и `python -c "..."` создавали/искали файл сессии в РАЗНЫХ местах
    ("bot/", "scripts/", "."), каждый раз получая пустую, неавторизованную
    сессию — и /backfill бесконечно падал с EOFError при попытке
    интерактивного логина в контейнере без TTY.

    run_backfill обязан передавать явный workdir=_PROJECT_ROOT, который не
    зависит от точки входа. Этот тест фиксирует, что _PROJECT_ROOT указывает
    на корень репозитория (где реально лежит yuvi_backfill_session.session),
    а не куда-то ещё.
    """
    assert _PROJECT_ROOT.is_dir()
    assert (_PROJECT_ROOT / "bot").is_dir()
    assert (_PROJECT_ROOT / "scripts").is_dir()
    assert (_PROJECT_ROOT / "requirements.txt").is_file()


# --- _pyrogram_message_to_row: TELEGRAM_SERVICE_ACCOUNT_ID (777000) --------


def _fake_pyrogram_message(
    *,
    message_id: int = 12345,
    from_user_id: int = 900010,
    from_user_is_bot: bool = False,
    text: str | None = "тестовое сообщение",
) -> SimpleNamespace:
    """Минимальный pyrogram.types.Message-подобный объект — только атрибуты,
    реально читаемые _pyrogram_message_to_row/_extract_pyrogram_media/
    _pyrogram_content_type."""
    return SimpleNamespace(
        service=None,
        empty=False,
        id=message_id,
        from_user=SimpleNamespace(
            id=from_user_id,
            is_bot=from_user_is_bot,
            username="backfill_user",
            first_name="Бэкфилл",
        ),
        text=text,
        caption=None,
        photo=None,
        video=None,
        voice=None,
        audio=None,
        document=None,
        sticker=None,
        reply_to_message_id=None,
        message_thread_id=None,
        forward_origin=None,
        date=datetime(2025, 1, 1, 12, 0, tzinfo=timezone.utc),
    )


def test_pyrogram_message_to_row_converts_real_user_message():
    """Регрессия/сравнение: обычное сообщение реального пользователя
    конвертируется в row-dict как обычно (baseline для тестов ниже)."""
    message = _fake_pyrogram_message()

    row = _pyrogram_message_to_row(message, chat_id=-100555010)

    assert row is not None
    assert row["user_id"] == 900010
    assert row["telegram_message_id"] == 12345


def test_pyrogram_message_to_row_excludes_telegram_service_account():
    """message.from_user.id == 777000 — реальный Message с is_bot=False на
    проводе (служебный аккаунт привязанного канала), поэтому обычная
    is_bot-проверка его не ловит: _pyrogram_message_to_row обязан вернуть
    None явной проверкой TELEGRAM_SERVICE_ACCOUNT_ID."""
    message = _fake_pyrogram_message(
        from_user_id=TELEGRAM_SERVICE_ACCOUNT_ID, from_user_is_bot=False
    )

    row = _pyrogram_message_to_row(message, chat_id=-100555011)

    assert row is None


def test_pyrogram_message_to_row_excludes_bots():
    """Пред-существующая проверка (не часть этого фикса) — сообщение бота
    по-прежнему пропускается."""
    message = _fake_pyrogram_message(from_user_id=900011, from_user_is_bot=True)

    row = _pyrogram_message_to_row(message, chat_id=-100555012)

    assert row is None


def test_pyrogram_message_to_row_excludes_missing_from_user():
    """Пред-существующая проверка (Pitfall 5) — служебные сообщения без
    автора (join/leave и т.п.) по-прежнему пропускаются."""
    message = _fake_pyrogram_message()
    message.from_user = None

    row = _pyrogram_message_to_row(message, chat_id=-100555013)

    assert row is None
