"""Тесты bot/services/pinned_menu_service.py (D-01/D-02/D-03, Task 3).

Bot полностью замокан (`AsyncMock` — `get_me`/`get_chat`/`send_message`/
`pin_chat_message`, без сети/реального aiogram). `bot_settings` — живой
Postgres через фикстуру `session` (транзакция-на-тест, `settings_service.
clear_cache()` в начале каждого теста) — тот же паттерн, что уже установлен
`test_settings_service.py`.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from bot.services import pinned_menu_service
from bot.services import settings_service


def _make_bot(pinned_message_id: int | None = None) -> AsyncMock:
    bot = AsyncMock()
    bot.get_me.return_value = SimpleNamespace(id=999999, username="yuvi_test_bot")
    pinned_message = SimpleNamespace(message_id=pinned_message_id) if pinned_message_id else None
    bot.get_chat.return_value = SimpleNamespace(pinned_message=pinned_message)
    bot.send_message.return_value = SimpleNamespace(message_id=555555)
    return bot


@pytest.mark.asyncio
async def test_ensure_pinned_menu_sends_and_pins_when_no_stored_pin(session):
    settings_service.clear_cache()
    chat_id = -800201
    bot = _make_bot(pinned_message_id=None)

    await pinned_menu_service.ensure_pinned_menu(bot, session, chat_id)

    bot.send_message.assert_awaited_once()
    bot.pin_chat_message.assert_awaited_once_with(chat_id, 555555, disable_notification=True)

    stored = await settings_service.get_setting(
        session, chat_id, pinned_menu_service.PINNED_MESSAGE_KEY, default=""
    )
    assert stored == "555555"


@pytest.mark.asyncio
async def test_ensure_pinned_menu_is_idempotent_when_already_pinned(session):
    settings_service.clear_cache()
    chat_id = -800202
    bot = _make_bot(pinned_message_id=None)

    # Первый вызов: нет сохранённого pin -> постит и закрепляет.
    await pinned_menu_service.ensure_pinned_menu(bot, session, chat_id)
    assert bot.send_message.await_count == 1

    # Второй вызов: getChat().pinned_message теперь совпадает с сохранённым id
    # -> не постит и не закрепляет повторно (D-02).
    bot.get_chat.return_value = SimpleNamespace(pinned_message=SimpleNamespace(message_id=555555))
    await pinned_menu_service.ensure_pinned_menu(bot, session, chat_id)

    assert bot.send_message.await_count == 1
    assert bot.pin_chat_message.await_count == 1


@pytest.mark.asyncio
async def test_ensure_pinned_menu_reposts_when_chat_lookup_fails(session):
    """TelegramBadRequest на getChat (сообщение удалено/чат недоступен) —
    трактуется как «нужно запостить заново», а не как ошибка/исключение наружу."""
    from aiogram.exceptions import TelegramBadRequest

    settings_service.clear_cache()
    chat_id = -800203
    bot = _make_bot(pinned_message_id=None)
    # Симулируем уже сохранённый (устаревший) message_id из прошлого запуска.
    await settings_service.set_setting(
        session, chat_id, pinned_menu_service.PINNED_MESSAGE_KEY, "111111", updated_by_tg_id=999999
    )
    bot.get_chat.side_effect = TelegramBadRequest(method=AsyncMock(), message="chat not found")

    await pinned_menu_service.ensure_pinned_menu(bot, session, chat_id)

    bot.send_message.assert_awaited_once()
    bot.pin_chat_message.assert_awaited_once()
