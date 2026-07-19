"""Wave 0 тесты /fb-команды (FEEDBACK-01, D-13) — bot/handlers/feedback_bot.py.

Парсинг опционального префикса категории (Claude's discretion, D-13), submit
через feedback_service.submit — автор строго из message.from_user, не из
текста заявки; reply-подсказка на пустой аргумент без создания заявки. Форма
мок-Message — SimpleNamespace, тот же паттерн, что tests/test_economy_handlers.py;
session-фикстура живого Postgres — тот же паттерн, что tests/test_feedback_service.py.

RED (Task 1): bot.handlers.feedback_bot ещё не существует — ImportError.
Реализация — Task 2 (GREEN).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from bot.handlers import feedback_bot
from bot.services import feedback_service
from common.models.user import User

CHAT_A = -900801


async def _ensure_user(session, user_id: int, first_name: str = "Тест") -> None:
    session.add(User(id=user_id, first_name=first_name))
    await session.flush()


def _fake_message(chat_id: int, user_id: int, first_name: str):
    """Минимальный aiogram-подобный Message — только атрибуты, которые
    реально читает cmd_fb."""
    return SimpleNamespace(
        chat=SimpleNamespace(id=chat_id),
        from_user=SimpleNamespace(id=user_id, first_name=first_name),
        reply=AsyncMock(),
    )


def _fake_command(args: str | None):
    return SimpleNamespace(args=args)


# --- cmd_fb: submit + автор из message.from_user (D-13) ---------------------


@pytest.mark.asyncio
async def test_fb_submits_via_service(session):
    user_id = 970701
    await _ensure_user(session, user_id, "Отправитель")

    message = _fake_message(CHAT_A, user_id, "Отправитель")
    command = _fake_command("нашёл баг X")

    await feedback_bot.cmd_fb(message, session, command)
    await session.commit()

    message.reply.assert_awaited_once()

    rows = await feedback_service.list_feedback(session, CHAT_A)
    matching = [row for row in rows if row["user_id"] == user_id]
    assert len(matching) == 1
    assert matching[0]["text"] == "нашёл баг X"


@pytest.mark.asyncio
async def test_fb_empty_args(session):
    user_id = 970802
    await _ensure_user(session, user_id, "Молчун")

    message = _fake_message(CHAT_A, user_id, "Молчун")
    command = _fake_command(None)

    await feedback_bot.cmd_fb(message, session, command)

    message.reply.assert_awaited_once()
    rows = await feedback_service.list_feedback(session, CHAT_A)
    assert all(row["user_id"] != user_id for row in rows)


# --- _parse_fb_args: опциональная категория (D-13, discretion) --------------


def test_fb_parses_category():
    assert feedback_bot._parse_fb_args("bug текст") == ("bug", "текст")
    assert feedback_bot._parse_fb_args("просто текст") == ("other", "просто текст")
