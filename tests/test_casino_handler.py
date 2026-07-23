"""Тесты bot/handlers/casino.py — /casino доступен по запросу, а не только
один раз при старте бота (см. pinned_menu_service.py)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from bot.config import settings
from bot.handlers import casino
from bot.services.pinned_menu_service import casino_message_content


@pytest.mark.asyncio
async def test_casino_command_sends_button_every_time() -> None:
    bot = AsyncMock()
    bot.get_me.return_value = SimpleNamespace(username="yuvi_test_bot")
    message = AsyncMock()

    await casino.casino_command(message, bot)
    await casino.casino_command(message, bot)

    expected_text, expected_keyboard = casino_message_content("yuvi_test_bot", settings.chat_id)
    assert message.answer.await_count == 2
    for call in message.answer.await_args_list:
        assert call.args[0] == expected_text
        assert call.kwargs["reply_markup"] == expected_keyboard
        assert call.kwargs["parse_mode"] == "HTML"
