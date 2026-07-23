"""Интеграционные тесты bot/handlers/economy.py против живого Postgres
(фикстура `session` из tests/conftest.py) + юнит-тесты чистых format_*
функций. Доказывает ECON-02: /balance /transfer /leaderboard /economy /rules
отвечают корректно прямо в группе, все пользовательские имена экранируются
перед HTML-выводом (T-03-06).
"""

from __future__ import annotations

from datetime import datetime
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import bot.handlers.economy as economy_handlers
from bot.config import settings
from bot.services import economy_service
from common.models.market import Market
from common.models.user import User


async def _ensure_user(session, user_id: int, first_name: str = "Тест") -> None:
    session.add(User(id=user_id, first_name=first_name))
    await session.flush()


def _fake_message(
    chat_id: int,
    user_id: int,
    first_name: str,
    text: str,
    *,
    message_id: int = 1,
    reply_to_message=None,
    entities=None,
):
    """Минимальный aiogram-подобный Message для тестов тонких хендлеров —
    только атрибуты, которые реально читают хендлеры economy.py."""
    return SimpleNamespace(
        chat=SimpleNamespace(id=chat_id),
        from_user=SimpleNamespace(id=user_id, first_name=first_name),
        message_id=message_id,
        text=text,
        reply_to_message=reply_to_message,
        entities=entities,
        answer=AsyncMock(),
    )


# --- /balance (ECON-01/02) ---------------------------------------------------


@pytest.mark.asyncio
async def test_balance_command_grants_start_bonus_for_newcomer(session):
    chat_id = -100800001
    user_id = 810001
    await _ensure_user(session, user_id, "Игрок")

    message = _fake_message(chat_id, user_id, "Игрок", "/balance")
    await economy_handlers.balance_command(message, session)

    message.answer.assert_awaited_once()
    text = message.answer.await_args.args[0]
    assert str(settings.economy_start_bonus) in text
    assert "ювик" in text


# --- /transfer (ECON-02, D-04) -----------------------------------------------


@pytest.mark.asyncio
async def test_transfer_command_reply_moves_money_with_fee(session):
    chat_id = -100800002
    sender_id, receiver_id = 810010, 810011
    await _ensure_user(session, sender_id, "Отправитель")
    await _ensure_user(session, receiver_id, "Получатель")
    await economy_service.get_balance(session, chat_id, sender_id)
    await economy_service.get_balance(session, chat_id, receiver_id)

    reply_to = SimpleNamespace(from_user=SimpleNamespace(id=receiver_id, first_name="Получатель"))
    message = _fake_message(
        chat_id, sender_id, "Отправитель", "/transfer 100",
        message_id=555, reply_to_message=reply_to,
    )

    await economy_handlers.transfer_command(message, session)

    message.answer.assert_awaited_once()
    text = message.answer.await_args.args[0]
    assert "Получатель" in text
    assert "5" in text  # комиссия max(1, ceil(0.05*100))=5

    receiver_balance = await economy_service.get_balance(session, chat_id, receiver_id)
    assert receiver_balance == settings.economy_start_bonus + 95


@pytest.mark.asyncio
async def test_transfer_command_no_target_gives_usage_hint(session):
    chat_id = -100800003
    sender_id = 810020
    await _ensure_user(session, sender_id, "Отправитель")
    await economy_service.get_balance(session, chat_id, sender_id)

    message = _fake_message(chat_id, sender_id, "Отправитель", "/transfer 100")
    await economy_handlers.transfer_command(message, session)

    text = message.answer.await_args.args[0]
    assert "Ответьте" in text or "ответьте" in text.lower()


@pytest.mark.asyncio
async def test_transfer_command_insufficient_funds_gives_clear_message(session):
    chat_id = -100800004
    sender_id, receiver_id = 810030, 810031
    await _ensure_user(session, sender_id, "Отправитель")
    await _ensure_user(session, receiver_id, "Получатель")
    await economy_service.get_balance(session, chat_id, sender_id)
    await economy_service.get_balance(session, chat_id, receiver_id)

    reply_to = SimpleNamespace(from_user=SimpleNamespace(id=receiver_id, first_name="Получатель"))
    message = _fake_message(
        chat_id, sender_id, "Отправитель", "/transfer 999999",
        message_id=556, reply_to_message=reply_to,
    )

    await economy_handlers.transfer_command(message, session)

    text = message.answer.await_args.args[0]
    assert "Недостаточно" in text


@pytest.mark.asyncio
async def test_transfer_command_self_transfer_gives_clear_message(session):
    chat_id = -100800007
    user_id = 810060
    await _ensure_user(session, user_id, "Соло")
    await economy_service.get_balance(session, chat_id, user_id)

    reply_to = SimpleNamespace(from_user=SimpleNamespace(id=user_id, first_name="Соло"))
    message = _fake_message(
        chat_id, user_id, "Соло", "/transfer 10",
        message_id=557, reply_to_message=reply_to,
    )

    await economy_handlers.transfer_command(message, session)

    text = message.answer.await_args.args[0]
    assert "самому себе" in text.lower()


# --- /leaderboard, /economy (D-06) -------------------------------------------


@pytest.mark.asyncio
async def test_leaderboard_command_lists_balances(session):
    chat_id = -100800005
    u1 = 810040
    await _ensure_user(session, u1, "Топ Игрок")
    await economy_service.get_balance(session, chat_id, u1)

    message = _fake_message(chat_id, u1, "Топ Игрок", "/leaderboard")
    await economy_handlers.leaderboard_command(message, session)

    text = message.answer.await_args.args[0]
    assert "Топ Игрок" in text
    assert str(settings.economy_start_bonus) in text


@pytest.mark.asyncio
async def test_economy_command_shows_bank_circulation_and_open_markets(session):
    chat_id = -100800006
    u1, u2 = 810050, 810051
    await _ensure_user(session, u1)
    await _ensure_user(session, u2)
    await economy_service.get_balance(session, chat_id, u1)
    await economy_service.get_balance(session, chat_id, u2)
    await economy_service.transfer_with_fee(session, chat_id, u1, u2, 100, "test_handler_economy_transfer")

    market = Market(
        chat_id=chat_id,
        type="internal",
        question="Рынок для теста /economy?",
        creator_id=u1,
        status="open",
        closes_at=datetime.utcnow() + timedelta(days=1),
    )
    session.add(market)
    await session.flush()

    message = _fake_message(chat_id, u1, "Тест", "/economy")
    await economy_handlers.economy_command(message, session)

    text = message.answer.await_args.args[0]
    assert "Банк" in text
    assert "5" in text  # комиссия перевода осевшая в банке
    assert "1" in text  # 1 открытый рынок


# --- /rules -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rules_command_describes_only_phase3_mechanics(session):
    message = _fake_message(-1, 1, "Тест", "/rules")
    await economy_handlers.rules_command(message)

    text = message.answer.await_args.args[0]
    for forbidden in ("казино", "гач", "дуэл"):
        assert forbidden not in text.lower()
    assert "ювик" in text.lower()


# --- Юнит-тесты чистых format_* (html.escape) --------------------------------


def test_format_leaderboard_escapes_html_in_names():
    rows = [{"user_id": 1, "first_name": "<b>x", "balance": 100}]
    text = economy_handlers.format_leaderboard(rows)
    assert "<b>x" not in text
    assert "&lt;b&gt;x" in text


def test_format_transfer_success_uses_already_escaped_name():
    text = economy_handlers.format_transfer_success(100, "&lt;b&gt;x", 5)
    assert "&lt;b&gt;x" in text
    assert "100" in text
    assert "5" in text


def test_plural_yuviki_forms():
    assert economy_handlers.plural_yuviki(1) == "ювик"
    assert economy_handlers.plural_yuviki(2) == "ювика"
    assert economy_handlers.plural_yuviki(5) == "ювиков"
    assert economy_handlers.plural_yuviki(11) == "ювиков"
    assert economy_handlers.plural_yuviki(21) == "ювик"


def test_format_chat_summary_contains_all_three_fields():
    summary = {"bank_balance": 50, "total_in_circulation": 2000, "open_markets_count": 3}
    text = economy_handlers.format_chat_summary(summary)
    assert "50" in text
    assert "2000" in text
    assert "3" in text
