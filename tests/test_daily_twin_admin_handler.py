"""Тесты admin-рубильника фичи "дневной двойник" целиком
(bot/handlers/daily_twin_admin.py, TWIN-03, запрошено 2026-07-29 — живой
инцидент со спамом реактивных ответов). ChatAdminFilter — внешний по
отношению к самим функциям хендлеров (см. test_ai_admin_handler.py), поэтому
здесь, как и там, зовём функции напрямую, без мока bot.get_chat_member.

Доказывают:
- /daily_twin_off персистентно выключает фичу (bot_settings через
  daily_twin_service.is_enabled), /daily_twin_on включает обратно.
- Выключенная фича гасит ОБА пути: проактивный тик
  (maybe_post_proactively) и реактивные ответы (daily_twin_reaction,
  bot/handlers/daily_twin.py) — даже когда всё остальное совпадает бы для
  выигрышного/матчащего сценария."""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.dispatcher.event.bases import SkipHandler

import bot.handlers.daily_twin as daily_twin_handlers
import bot.handlers.daily_twin_admin as daily_twin_admin_handlers
from bot.services import daily_twin_service
from bot.services import twin_service
from common.models.twin_opt_in import TwinOptIn
from common.models.user import User


async def _ensure_user(session, user_id: int, first_name: str = "Тест") -> None:
    session.add(User(id=user_id, first_name=first_name))
    await session.flush()


async def _set_opt_in(session, chat_id: int, user_id: int, status: str) -> None:
    session.add(TwinOptIn(chat_id=chat_id, user_id=user_id, status=status))
    await session.flush()


def _fake_admin_message(chat_id: int, admin_id: int = 900_001):
    return SimpleNamespace(
        chat=SimpleNamespace(id=chat_id),
        from_user=SimpleNamespace(id=admin_id),
        answer=AsyncMock(),
    )


class _FixedDatetime(datetime):
    _fixed_hour = 13

    @classmethod
    def now(cls, tz=None):
        return datetime(2026, 7, 29, cls._fixed_hour, 0, tzinfo=tz)


async def _fake_stream(messages: list[dict], model: str, max_tokens: int):
    yield "норм чё"


def _fake_bot():
    return SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=1)))


@pytest.mark.asyncio
async def test_daily_twin_off_persists_and_replies(session):
    chat_id = -100942001

    message = _fake_admin_message(chat_id)
    await daily_twin_admin_handlers.daily_twin_off_command(message, session)

    message.answer.assert_awaited_once()
    assert await daily_twin_service.is_enabled(session, chat_id) is False


@pytest.mark.asyncio
async def test_daily_twin_on_re_enables(session):
    chat_id = -100942002
    await daily_twin_service.set_enabled(session, chat_id, False, updated_by_tg_id=900_002)
    await session.commit()
    assert await daily_twin_service.is_enabled(session, chat_id) is False

    message = _fake_admin_message(chat_id)
    await daily_twin_admin_handlers.daily_twin_on_command(message, session)

    assert await daily_twin_service.is_enabled(session, chat_id) is True


@pytest.mark.asyncio
async def test_no_row_means_enabled_by_default(session):
    """Обратная совместимость — чат, где рубильник ни разу не трогали,
    ведёт себя как раньше (фича включена)."""
    chat_id = -100942003
    assert await daily_twin_service.is_enabled(session, chat_id) is True


@pytest.mark.asyncio
async def test_disabled_feature_skips_proactive_tick(session, monkeypatch):
    """Выключенная фича гасит проактивный тик даже когда всё остальное
    сложилось бы для поста (окно/кандидат/монета выпала)."""
    monkeypatch.setattr(daily_twin_service, "datetime", _FixedDatetime)
    monkeypatch.setattr(daily_twin_service, "_tick_probability", lambda: 1.0)
    chat_id = -100942004
    user_id = 942004
    await _ensure_user(session, user_id, "Тест")
    await _set_opt_in(session, chat_id, user_id, "active")
    await daily_twin_service.set_enabled(session, chat_id, False, updated_by_tg_id=900_004)
    await session.commit()
    bot = _fake_bot()

    posted = await daily_twin_service.maybe_post_proactively(session, chat_id, bot)

    assert posted is False
    bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_disabled_feature_skips_reactive_reply(session, monkeypatch):
    """Выключенная фича гасит реактивный ответ даже на реплай ПРЯМО на
    пост двойника (обычно самый однозначный матч из всех)."""
    monkeypatch.setattr(twin_service.ai_client, "stream", _fake_stream)

    chat_id = -100942005
    target_id = 942005
    twin_post_message_id = 800_700
    await _ensure_user(session, target_id, "Дима")
    await _set_opt_in(session, chat_id, target_id, "active")
    await daily_twin_service.record_post(session, chat_id, twin_post_message_id, target_id)
    await daily_twin_service.set_enabled(session, chat_id, False, updated_by_tg_id=900_005)
    await session.commit()

    message = SimpleNamespace(
        chat=SimpleNamespace(id=chat_id),
        from_user=SimpleNamespace(id=942006, first_name="Кто-то"),
        text="го ещё раз",
        reply_to_message=SimpleNamespace(
            message_id=twin_post_message_id, from_user=SimpleNamespace(id=target_id)
        ),
        entities=None,
        answer=AsyncMock(),
    )

    with pytest.raises(SkipHandler):
        await daily_twin_handlers.daily_twin_reaction(message, session)

    message.answer.assert_not_awaited()
