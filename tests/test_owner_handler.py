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
from sqlalchemy import select

import bot.handlers.owner as owner_handlers
from bot.config import settings
from bot.services import changelog_service
from bot.services import economy_service
from bot.services import jackpot_service
from bot.services import lurker_service
from common.models.slot_jackpot import SlotJackpot
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


# --- /giveaway -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_giveaway_refuses_non_owner(session):
    chat_id = -100930021
    non_owner_id, other_id = 930028, 930029
    assert non_owner_id != settings.owner_id
    await _ensure_user(session, non_owner_id, "Не владелец")
    await _ensure_user(session, other_id, "Участник")
    await economy_service.get_balance(session, chat_id, other_id)

    message = _fake_message(chat_id, non_owner_id, "Не владелец", "/giveaway 100")
    await owner_handlers.giveaway_command(message, session)

    message.reply.assert_awaited_once()
    assert "владельцу" in message.reply.await_args.args[0].lower()

    balance = await economy_service.get_balance(session, chat_id, other_id)
    assert balance == settings.economy_start_bonus


@pytest.mark.asyncio
async def test_giveaway_credits_random_participant_for_owner(session, monkeypatch):
    monkeypatch.setattr(settings, "owner_id", 930030)
    chat_id = -100930022
    user_a, user_b = 930031, 930032
    await _ensure_user(session, user_a, "Первый")
    await _ensure_user(session, user_b, "Второй")
    await economy_service.get_balance(session, chat_id, user_a)
    await economy_service.get_balance(session, chat_id, user_b)

    message = _fake_message(chat_id, 930030, "Владелец", "/giveaway 300")
    await owner_handlers.giveaway_command(message, session)

    message.answer.assert_awaited_once()
    text = message.answer.await_args.args[0]
    assert "300" in text

    balance_a = await economy_service.get_balance(session, chat_id, user_a)
    balance_b = await economy_service.get_balance(session, chat_id, user_b)
    # Ровно один из двух получил приз (реальный RNG, как у /grant/grant_all —
    # не форсируем, но проверяем, что деньги реально сдвинулись у ОДНОГО).
    winners = [
        uid
        for uid, bal in ((user_a, balance_a), (user_b, balance_b))
        if bal == settings.economy_start_bonus + 300
    ]
    assert len(winners) == 1


@pytest.mark.asyncio
async def test_giveaway_invalid_args_gives_usage_hint(session, monkeypatch):
    monkeypatch.setattr(settings, "owner_id", 930033)
    chat_id = -100930023
    await _ensure_user(session, 930033, "Владелец")

    message = _fake_message(chat_id, 930033, "Владелец", "/giveaway not_a_number")
    await owner_handlers.giveaway_command(message, session)

    message.answer.assert_awaited_once()
    assert "использование" in message.answer.await_args.args[0].lower()


@pytest.mark.asyncio
async def test_giveaway_no_participants_reports_nobody_to_pick(session, monkeypatch):
    monkeypatch.setattr(settings, "owner_id", 930034)
    chat_id = -100930024
    await _ensure_user(session, 930034, "Владелец")
    # Владелец сам НЕ участник экономики этого чата — UserBalance пуст.

    message = _fake_message(chat_id, 930034, "Владелец", "/giveaway 100")
    await owner_handlers.giveaway_command(message, session)

    message.answer.assert_awaited_once()
    assert "не для кого" in message.answer.await_args.args[0].lower()


@pytest.mark.asyncio
async def test_giveaway_replayed_message_shows_same_winner(session, monkeypatch):
    monkeypatch.setattr(settings, "owner_id", 930035)
    chat_id = -100930025
    user_a = 930036
    await _ensure_user(session, user_a, "Единственный")
    await economy_service.get_balance(session, chat_id, user_a)

    # Единственный участник экономики -> победитель однозначен без монкипатча
    # RNG на уровне хендлера (RNG форсируется отдельно в test_giveaway_service.py).
    message = _fake_message(chat_id, 930035, "Владелец", "/giveaway 150", message_id=88)
    await owner_handlers.giveaway_command(message, session)
    await owner_handlers.giveaway_command(message, session)  # same message_id -> same ref_id

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


# --- /jackpot_event --------------------------------------------------------------


async def _get_jackpot_row(session, chat_id: int) -> SlotJackpot | None:
    return (
        await session.execute(select(SlotJackpot).where(SlotJackpot.chat_id == chat_id))
    ).scalar_one_or_none()


@pytest.mark.asyncio
async def test_jackpot_event_refuses_non_owner(session):
    chat_id = -100930016
    non_owner_id = 930040
    assert non_owner_id != settings.owner_id
    await _ensure_user(session, non_owner_id, "Не владелец")

    message = _fake_message(chat_id, non_owner_id, "Не владелец", "/jackpot_event")
    await owner_handlers.jackpot_event_command(message, session)

    message.reply.assert_awaited_once()
    assert "владельцу" in message.reply.await_args.args[0].lower()
    message.answer.assert_not_awaited()
    assert await _get_jackpot_row(session, chat_id) is None


@pytest.mark.asyncio
async def test_jackpot_event_no_args_defaults_to_100_spins_for_owner(session, monkeypatch):
    monkeypatch.setattr(settings, "owner_id", 930041)
    chat_id = -100930017
    await _ensure_user(session, 930041, "Владелец")

    message = _fake_message(chat_id, 930041, "Владелец", "/jackpot_event")
    await owner_handlers.jackpot_event_command(message, session)

    message.answer.assert_awaited_once()
    text = message.answer.await_args.args[0]
    assert "100" in text
    assert jackpot_service.EVENT_NAME in text

    row = await _get_jackpot_row(session, chat_id)
    assert row is not None
    assert row.event_spins_remaining == 100


@pytest.mark.asyncio
async def test_jackpot_event_explicit_spins_for_owner(session, monkeypatch):
    monkeypatch.setattr(settings, "owner_id", 930042)
    chat_id = -100930018
    await _ensure_user(session, 930042, "Владелец")

    message = _fake_message(chat_id, 930042, "Владелец", "/jackpot_event 250")
    await owner_handlers.jackpot_event_command(message, session)

    message.answer.assert_awaited_once()
    text = message.answer.await_args.args[0]
    assert "250" in text

    row = await _get_jackpot_row(session, chat_id)
    assert row.event_spins_remaining == 250


@pytest.mark.asyncio
async def test_jackpot_event_invalid_args_gives_usage_hint_and_touches_nothing(session, monkeypatch):
    monkeypatch.setattr(settings, "owner_id", 930043)
    chat_id = -100930019
    await _ensure_user(session, 930043, "Владелец")

    # "--5"/"---3" — регресс, найденный ревью 2026-08-07:
    # `.lstrip("-").isdigit()` ошибочно пропускал их (лишние минусы
    # схлопывались), а `int("--5")` падал необработанным ValueError.
    for bad_args in [
        "/jackpot_event 0",
        "/jackpot_event -5",
        "/jackpot_event --5",
        "/jackpot_event ---3",
        "/jackpot_event abc",
        "/jackpot_event 1 2",
    ]:
        message = _fake_message(chat_id, 930043, "Владелец", bad_args)
        await owner_handlers.jackpot_event_command(message, session)
        message.answer.assert_awaited_once()
        assert "Использование" in message.answer.await_args.args[0]

    assert await _get_jackpot_row(session, chat_id) is None


@pytest.mark.asyncio
async def test_jackpot_event_restarting_overrides_previous_count(session, monkeypatch):
    """Владелец сам решает перезапустить ивент — команда не отказывает,
    просто перезаписывает remaining новым значением (см. докстринг
    jackpot_service.start_event)."""
    monkeypatch.setattr(settings, "owner_id", 930044)
    chat_id = -100930020
    await _ensure_user(session, 930044, "Владелец")

    first = _fake_message(chat_id, 930044, "Владелец", "/jackpot_event 100")
    await owner_handlers.jackpot_event_command(first, session)
    assert (await _get_jackpot_row(session, chat_id)).event_spins_remaining == 100

    second = _fake_message(chat_id, 930044, "Владелец", "/jackpot_event 30")
    await owner_handlers.jackpot_event_command(second, session)
    assert (await _get_jackpot_row(session, chat_id)).event_spins_remaining == 30
