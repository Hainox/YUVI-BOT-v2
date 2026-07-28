"""Тесты /grant и /post_update (bot/handlers/owner.py) против живого Postgres
(фикстура `session` из conftest.py) + мок Message (форма test_farm_admin.py).
Доказывает: обе команды отказывают не-владельцу (settings.owner_id), даже
если у него есть права админа чата — в отличие от /farmwipe это НЕ
чат-специфичный гейт.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import bot.handlers.owner as owner_handlers
from bot.config import settings
from bot.services import changelog_service
from bot.services import economy_service
from bot.services import jackpot_service
from bot.services import lurker_service
from common.models.user import User


async def _ensure_user(session, user_id: int, first_name: str = "Тест", username: str | None = None) -> None:
    session.add(User(id=user_id, first_name=first_name, username=username))
    await session.flush()


def _fake_message(chat_id: int, user_id: int, first_name: str, text: str, *, message_id: int = 1):
    return SimpleNamespace(
        chat=SimpleNamespace(id=chat_id),
        from_user=SimpleNamespace(id=user_id, first_name=first_name),
        message_id=message_id,
        text=text,
        answer=AsyncMock(),
        answer_animation=AsyncMock(),
        reply=AsyncMock(),
    )


def _fake_command(args: str | None):
    return SimpleNamespace(args=args)


# --- /grant ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_grant_refuses_non_owner(session):
    chat_id = -100930001
    non_owner_id, target_id = 930001, 930002
    assert non_owner_id != settings.owner_id
    await _ensure_user(session, non_owner_id, "Не владелец")
    await _ensure_user(session, target_id, "Цель")

    message = _fake_message(chat_id, non_owner_id, "Не владелец", "/grant 930002 100")
    await owner_handlers.grant_command(message, session)

    message.reply.assert_awaited_once()
    assert "владельцу" in message.reply.await_args.args[0].lower()

    # economy_service.get_balance get-or-creates on first access (start bonus) —
    # untouched by the rejected grant means exactly the start bonus, not 0.
    balance = await economy_service.get_balance(session, chat_id, target_id)
    assert balance == settings.economy_start_bonus


@pytest.mark.asyncio
async def test_grant_credits_target_for_owner(session, monkeypatch):
    monkeypatch.setattr(settings, "owner_id", 930003)
    chat_id = -100930002
    target_id = 930004
    await _ensure_user(session, target_id, "Цель")

    message = _fake_message(chat_id, 930003, "Владелец", f"/grant {target_id} 500")
    await owner_handlers.grant_command(message, session)

    message.answer.assert_awaited_once()
    text = message.answer.await_args.args[0]
    assert "500" in text

    balance = await economy_service.get_balance(session, chat_id, target_id)
    assert balance == settings.economy_start_bonus + 500


@pytest.mark.asyncio
async def test_grant_invalid_args_gives_usage_hint(session, monkeypatch):
    monkeypatch.setattr(settings, "owner_id", 930005)
    chat_id = -100930003
    await _ensure_user(session, 930005, "Владелец")

    message = _fake_message(chat_id, 930005, "Владелец", "/grant not_enough_args")
    await owner_handlers.grant_command(message, session)

    message.answer.assert_awaited_once()
    assert "использование" in message.answer.await_args.args[0].lower()


@pytest.mark.asyncio
async def test_grant_unknown_target_reports_not_found(session, monkeypatch):
    monkeypatch.setattr(settings, "owner_id", 930006)
    chat_id = -100930004
    await _ensure_user(session, 930006, "Владелец")

    message = _fake_message(chat_id, 930006, "Владелец", "/grant 999999999 100")
    await owner_handlers.grant_command(message, session)

    message.answer.assert_awaited_once()
    assert "не найден" in message.answer.await_args.args[0].lower()


@pytest.mark.asyncio
async def test_grant_replayed_ref_id_does_not_double_credit(session, monkeypatch):
    monkeypatch.setattr(settings, "owner_id", 930007)
    chat_id = -100930005
    target_id = 930008
    await _ensure_user(session, target_id, "Цель")

    message = _fake_message(chat_id, 930007, "Владелец", f"/grant {target_id} 200", message_id=42)
    await owner_handlers.grant_command(message, session)
    await owner_handlers.grant_command(message, session)  # same message_id -> same ref_id

    balance = await economy_service.get_balance(session, chat_id, target_id)
    assert balance == settings.economy_start_bonus + 200


# --- /grant_all ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_grant_all_refuses_non_owner(session):
    chat_id = -100930016
    non_owner_id, other_id = 930019, 930020
    assert non_owner_id != settings.owner_id
    await _ensure_user(session, non_owner_id, "Не владелец")
    await _ensure_user(session, other_id, "Участник")
    await economy_service.get_balance(session, chat_id, other_id)  # даёт other_id строку UserBalance

    message = _fake_message(chat_id, non_owner_id, "Не владелец", "/grant_all 100")
    await owner_handlers.grant_all_command(message, session)

    message.reply.assert_awaited_once()
    assert "владельцу" in message.reply.await_args.args[0].lower()

    balance = await economy_service.get_balance(session, chat_id, other_id)
    assert balance == settings.economy_start_bonus


@pytest.mark.asyncio
async def test_grant_all_credits_every_participant_for_owner(session, monkeypatch):
    monkeypatch.setattr(settings, "owner_id", 930021)
    chat_id = -100930017
    user_a, user_b = 930022, 930023
    await _ensure_user(session, user_a, "Первый")
    await _ensure_user(session, user_b, "Второй")
    # get_balance get-or-creates the UserBalance row (тот же критерий
    # "участник", что get_leaderboard) — до этого их нет в чате вообще.
    await economy_service.get_balance(session, chat_id, user_a)
    await economy_service.get_balance(session, chat_id, user_b)

    message = _fake_message(chat_id, 930021, "Владелец", "/grant_all 300")
    await owner_handlers.grant_all_command(message, session)

    message.answer.assert_awaited_once()
    text = message.answer.await_args.args[0]
    assert "300" in text
    assert "2" in text  # "2 из 2"

    assert await economy_service.get_balance(session, chat_id, user_a) == settings.economy_start_bonus + 300
    assert await economy_service.get_balance(session, chat_id, user_b) == settings.economy_start_bonus + 300


@pytest.mark.asyncio
async def test_grant_all_invalid_args_gives_usage_hint(session, monkeypatch):
    monkeypatch.setattr(settings, "owner_id", 930024)
    chat_id = -100930018
    await _ensure_user(session, 930024, "Владелец")

    message = _fake_message(chat_id, 930024, "Владелец", "/grant_all not_a_number")
    await owner_handlers.grant_all_command(message, session)

    message.answer.assert_awaited_once()
    assert "использование" in message.answer.await_args.args[0].lower()


@pytest.mark.asyncio
async def test_grant_all_no_participants_reports_nobody_to_credit(session, monkeypatch):
    monkeypatch.setattr(settings, "owner_id", 930025)
    chat_id = -100930019
    await _ensure_user(session, 930025, "Владелец")
    # Владелец сам НЕ участник экономики этого чата (никогда не звал
    # get_balance в нём) — UserBalance пуст для chat_id.

    message = _fake_message(chat_id, 930025, "Владелец", "/grant_all 100")
    await owner_handlers.grant_all_command(message, session)

    message.answer.assert_awaited_once()
    assert "некому" in message.answer.await_args.args[0].lower()


@pytest.mark.asyncio
async def test_grant_all_replayed_message_does_not_double_credit(session, monkeypatch):
    monkeypatch.setattr(settings, "owner_id", 930026)
    chat_id = -100930020
    user_a = 930027
    await _ensure_user(session, user_a, "Участник")
    await economy_service.get_balance(session, chat_id, user_a)

    message = _fake_message(chat_id, 930026, "Владелец", "/grant_all 150", message_id=77)
    await owner_handlers.grant_all_command(message, session)
    await owner_handlers.grant_all_command(message, session)  # same message_id -> same ref_id per user

    balance = await economy_service.get_balance(session, chat_id, user_a)
    assert balance == settings.economy_start_bonus + 150


# --- /post_update --------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_update_refuses_non_owner(session):
    chat_id = -100930006
    non_owner_id = 930009
    assert non_owner_id != settings.owner_id
    await _ensure_user(session, non_owner_id, "Не владелец")

    message = _fake_message(chat_id, non_owner_id, "Не владелец", "/post_update Заголовок")
    await owner_handlers.post_update_command(message, _fake_command("Заголовок"), session)

    message.reply.assert_awaited_once()
    assert "владельцу" in message.reply.await_args.args[0].lower()


@pytest.mark.asyncio
async def test_post_update_creates_entry_with_title_and_body(session, monkeypatch):
    monkeypatch.setattr(settings, "owner_id", 930010)
    chat_id = -100930007
    await _ensure_user(session, 930010, "Владелец")

    message = _fake_message(chat_id, 930010, "Владелец", "/post_update Заголовок\nТело записи")
    await owner_handlers.post_update_command(
        message, _fake_command("Заголовок\nТело записи"), session
    )

    message.answer.assert_awaited_once()
    assert "Заголовок" in message.answer.await_args.args[0]

    entries = await changelog_service.list_entries(session)
    assert entries[0].title == "Заголовок"
    assert entries[0].body == "Тело записи"


@pytest.mark.asyncio
async def test_post_update_title_only_leaves_body_none(session, monkeypatch):
    monkeypatch.setattr(settings, "owner_id", 930011)
    chat_id = -100930008
    await _ensure_user(session, 930011, "Владелец")

    message = _fake_message(chat_id, 930011, "Владелец", "/post_update Только заголовок")
    await owner_handlers.post_update_command(message, _fake_command("Только заголовок"), session)

    entries = await changelog_service.list_entries(session)
    assert entries[0].title == "Только заголовок"
    assert entries[0].body is None


@pytest.mark.asyncio
async def test_post_update_empty_args_gives_usage_hint(session, monkeypatch):
    monkeypatch.setattr(settings, "owner_id", 930012)
    chat_id = -100930009
    await _ensure_user(session, 930012, "Владелец")

    message = _fake_message(chat_id, 930012, "Владелец", "/post_update")
    await owner_handlers.post_update_command(message, _fake_command(None), session)

    message.answer.assert_awaited_once()
    assert "использование" in message.answer.await_args.args[0].lower()


# --- /test_jackpot -------------------------------------------------------------


@pytest.mark.asyncio
async def test_test_jackpot_refuses_non_owner(session):
    chat_id = -100930010
    non_owner_id = 930013
    assert non_owner_id != settings.owner_id
    await _ensure_user(session, non_owner_id, "Не владелец")

    message = _fake_message(chat_id, non_owner_id, "Не владелец", "/test_jackpot")
    await owner_handlers.test_jackpot_command(message, session)

    message.reply.assert_awaited_once()
    assert "владельцу" in message.reply.await_args.args[0].lower()
    message.answer_animation.assert_not_awaited()


@pytest.mark.asyncio
async def test_test_jackpot_sends_gif_with_current_pool_for_owner(session, monkeypatch):
    monkeypatch.setattr(settings, "owner_id", 930014)
    chat_id = -100930011
    await _ensure_user(session, 930014, "Владелец")

    message = _fake_message(chat_id, 930014, "Владелец", "/test_jackpot")
    await owner_handlers.test_jackpot_command(message, session)

    message.answer_animation.assert_awaited_once()
    caption = message.answer_animation.await_args.kwargs["caption"]
    assert str(settings.slot_jackpot_seed) in caption
    assert "Владелец" in caption


@pytest.mark.asyncio
async def test_test_jackpot_reports_missing_gif_file(session, monkeypatch):
    monkeypatch.setattr(settings, "owner_id", 930015)
    monkeypatch.setattr(jackpot_service, "JACKPOT_GIF_PATH", jackpot_service.JACKPOT_GIF_PATH.parent / "nope.gif")
    chat_id = -100930012
    await _ensure_user(session, 930015, "Владелец")

    message = _fake_message(chat_id, 930015, "Владелец", "/test_jackpot")
    await owner_handlers.test_jackpot_command(message, session)

    message.answer.assert_awaited_once()
    assert "не найден" in message.answer.await_args.args[0].lower()
    message.answer_animation.assert_not_awaited()


# --- /test_lurker ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_test_lurker_refuses_non_owner(session):
    chat_id = -100930013
    non_owner_id = 930016
    assert non_owner_id != settings.owner_id
    await _ensure_user(session, non_owner_id, "Не владелец")

    message = _fake_message(chat_id, non_owner_id, "Не владелец", "/test_lurker")
    await owner_handlers.test_lurker_command(message, session)

    message.reply.assert_awaited_once()
    assert "владельцу" in message.reply.await_args.args[0].lower()
    message.answer.assert_not_awaited()


@pytest.mark.asyncio
async def test_test_lurker_sends_generated_message_for_owner(session, monkeypatch):
    monkeypatch.setattr(settings, "owner_id", 930017)
    chat_id = -100930014
    await _ensure_user(session, 930017, "Владелец")
    monkeypatch.setattr(
        lurker_service, "build_daily_message", AsyncMock(return_value="Тестовый текст про Дениску\n\n#мем")
    )

    message = _fake_message(chat_id, 930017, "Владелец", "/test_lurker")
    await owner_handlers.test_lurker_command(message, session)

    message.answer.assert_awaited_once_with("Тестовый текст про Дениску\n\n#мем")


@pytest.mark.asyncio
async def test_test_lurker_reports_empty_llm_response(session, monkeypatch):
    monkeypatch.setattr(settings, "owner_id", 930018)
    chat_id = -100930015
    await _ensure_user(session, 930018, "Владелец")
    monkeypatch.setattr(lurker_service, "build_daily_message", AsyncMock(return_value=""))

    message = _fake_message(chat_id, 930018, "Владелец", "/test_lurker")
    await owner_handlers.test_lurker_command(message, session)

    message.answer.assert_awaited_once()
    assert "пуст" in message.answer.await_args.args[0].lower()
