"""Тесты admin-рубильника фичи скачивания медиа целиком
(bot/handlers/media_dl_admin.py, запрошено 2026-07-30). ChatAdminFilter —
внешний по отношению к самим функциям хендлеров (см.
test_daily_twin_admin_handler.py), поэтому здесь тоже зовём функции напрямую,
без мока bot.get_chat_member.

Доказывают:
- /media_dl_off персистентно выключает фичу (bot_settings через
  media_dl_service.is_enabled), /media_dl_on включает обратно.
- Выключенная фича гасит on_media_url (bot/handlers/media_dl.py) ДО любого
  сетевого вызова к cobalt (resolve) и до списания денег.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import bot.handlers.media_dl as media_dl_handlers
import bot.handlers.media_dl_admin as media_dl_admin_handlers
from bot.services import media_dl_service


def _fake_admin_message(chat_id: int, admin_id: int = 910_001):
    return SimpleNamespace(
        chat=SimpleNamespace(id=chat_id),
        from_user=SimpleNamespace(id=admin_id),
        answer=AsyncMock(),
    )


@pytest.mark.asyncio
async def test_media_dl_off_persists_and_replies(session):
    chat_id = -100951001

    message = _fake_admin_message(chat_id)
    await media_dl_admin_handlers.media_dl_off_command(message, session)

    message.answer.assert_awaited_once()
    assert await media_dl_service.is_enabled(session, chat_id) is False


@pytest.mark.asyncio
async def test_media_dl_on_re_enables(session):
    chat_id = -100951002
    await media_dl_service.set_enabled(session, chat_id, False, updated_by_tg_id=910_002)
    await session.commit()
    assert await media_dl_service.is_enabled(session, chat_id) is False

    message = _fake_admin_message(chat_id)
    await media_dl_admin_handlers.media_dl_on_command(message, session)

    assert await media_dl_service.is_enabled(session, chat_id) is True


@pytest.mark.asyncio
async def test_no_row_means_enabled_by_default(session):
    """Обратная совместимость — чат, где рубильник ни разу не трогали,
    ведёт себя как раньше (фича включена)."""
    chat_id = -100951003
    assert await media_dl_service.is_enabled(session, chat_id) is True


@pytest.mark.asyncio
async def test_disabled_feature_skips_on_media_url(session, monkeypatch):
    """Выключенная фича гасит on_media_url ДО обращения к cobalt (resolve) и
    до списания денег — даже на валидную распознанную ссылку."""

    async def _fail_if_called(*args, **kwargs):
        raise AssertionError("resolve() не должен вызываться, пока фича выключена")

    monkeypatch.setattr(media_dl_service, "resolve", _fail_if_called)

    chat_id = -100951004
    user_id = 951004
    await media_dl_service.set_enabled(session, chat_id, False, updated_by_tg_id=910_004)
    await session.commit()

    message = SimpleNamespace(
        chat=SimpleNamespace(id=chat_id),
        from_user=SimpleNamespace(id=user_id, first_name="Тест"),
        message_id=1,
        text="глянь https://www.tiktok.com/@u/video/1",
        reply=AsyncMock(),
    )
    bot_mock = SimpleNamespace(send_video=AsyncMock(), send_media_group=AsyncMock(), delete_message=AsyncMock())

    await media_dl_handlers.on_media_url(message, session, bot_mock)

    message.reply.assert_not_awaited()
    bot_mock.send_video.assert_not_awaited()
