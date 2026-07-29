"""Тесты общего резолва цели (`bot/services/target_resolution_service.py`,
WR-04, 04.2-REVIEW) против живого Postgres (фикстура `session` из
tests/conftest.py — форма tests/test_economy_handlers.py/
tests/test_farm_admin.py: минимальный aiogram-подобный Message через
SimpleNamespace, только атрибуты, реально читаемые resolve_target).

Доказывает:
- resolve_by_username_or_id возвращает None для @username/id, резолвящихся в
  TELEGRAM_SERVICE_ACCOUNT_ID (777000, служебный аккаунт привязанного
  канала) — реально существующая строка users, но не валидная цель.
- resolve_by_username_or_id по-прежнему нормально резолвит реального
  пользователя и по @username, и по числовому id (регрессия).
- resolve_target: reply_to_message.from_user с id=777000 (is_bot=False на
  проводе — обычная is_bot-проверка его не ловит) падает дальше по цепочке
  резолва (text_mention -> @username/id-аргумент), а не сразу возвращает
  None, если дальше есть валидная цель.
- resolve_target: reply_to_message.from_user с is_bot=True (любой id) падает
  дальше по цепочке так же.
- resolve_target: text_mention entity с user.id=777000 падает дальше по
  цепочке так же.
- resolve_target: обычный валидный reply/text_mention/@username-аргумент
  по-прежнему резолвится (регрессия — WR-04 dedup в общий модуль не должен
  был сломать счастливый путь).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from bot.constants import TELEGRAM_SERVICE_ACCOUNT_ID
from bot.services import target_resolution_service
from common.models.user import User


async def _ensure_user(
    session, user_id: int, first_name: str = "Тест", username: str | None = None
) -> None:
    session.add(User(id=user_id, first_name=first_name, username=username))
    await session.flush()


def _entity(entity_type: str, user: SimpleNamespace | None = None) -> SimpleNamespace:
    """Минимальная aiogram-подобная MessageEntity (форма
    tests/test_daily_twin_handler.py::_entity) — только type/user, которые
    реально читает resolve_target."""
    return SimpleNamespace(type=entity_type, user=user)


def _fake_message(
    *,
    reply_to_message: SimpleNamespace | None = None,
    entities: list[SimpleNamespace] | None = None,
):
    """Минимальный aiogram-подобный Message — resolve_target читает только
    reply_to_message и entities (target_arg передаётся отдельным параметром,
    не через message)."""
    return SimpleNamespace(reply_to_message=reply_to_message, entities=entities)


def _fake_user(user_id: int, is_bot: bool = False, first_name: str = "Юзер") -> SimpleNamespace:
    return SimpleNamespace(id=user_id, is_bot=is_bot, first_name=first_name)


# --- resolve_by_username_or_id -----------------------------------------------


@pytest.mark.asyncio
async def test_resolve_by_username_or_id_returns_none_for_telegram_service_account_username(session):
    await _ensure_user(
        session, TELEGRAM_SERVICE_ACCOUNT_ID, "Telegram", username="tg_service_channel"
    )
    await session.commit()

    result = await target_resolution_service.resolve_by_username_or_id(
        session, "@tg_service_channel"
    )

    assert result is None


@pytest.mark.asyncio
async def test_resolve_by_username_or_id_returns_none_for_telegram_service_account_id(session):
    await _ensure_user(session, TELEGRAM_SERVICE_ACCOUNT_ID, "Telegram")
    await session.commit()

    result = await target_resolution_service.resolve_by_username_or_id(
        session, str(TELEGRAM_SERVICE_ACCOUNT_ID)
    )

    assert result is None


@pytest.mark.asyncio
async def test_resolve_by_username_or_id_still_resolves_real_user_by_username(session):
    real_id = 950101
    await _ensure_user(session, real_id, "Реальный", username="real_user")
    await session.commit()

    result = await target_resolution_service.resolve_by_username_or_id(session, "@real_user")

    assert result == (real_id, "Реальный")


@pytest.mark.asyncio
async def test_resolve_by_username_or_id_still_resolves_real_user_by_id(session):
    real_id = 950102
    await _ensure_user(session, real_id, "Реальный2")
    await session.commit()

    result = await target_resolution_service.resolve_by_username_or_id(session, str(real_id))

    assert result == (real_id, "Реальный2")


@pytest.mark.asyncio
async def test_resolve_by_username_or_id_returns_none_for_unknown_username(session):
    result = await target_resolution_service.resolve_by_username_or_id(session, "@nobody_here")

    assert result is None


@pytest.mark.asyncio
async def test_resolve_by_username_or_id_returns_none_for_non_username_non_digit_arg(session):
    result = await target_resolution_service.resolve_by_username_or_id(session, "not-an-id")

    assert result is None


# --- resolve_target: reply на служебный аккаунт / бота ----------------------


@pytest.mark.asyncio
async def test_resolve_target_falls_through_reply_to_telegram_service_account_no_fallback(session):
    """reply на 777000, никакого другого способа резолва — итог None (не
    возвращает служебный аккаунт как цель)."""
    reply_to = SimpleNamespace(
        from_user=_fake_user(TELEGRAM_SERVICE_ACCOUNT_ID, is_bot=False)
    )
    message = _fake_message(reply_to_message=reply_to)

    result = await target_resolution_service.resolve_target(message, session, None)

    assert result is None


@pytest.mark.asyncio
async def test_resolve_target_falls_through_reply_to_telegram_service_account_to_username_arg(session):
    """reply на 777000 + отдельный валидный @username-аргумент в том же
    сообщении — невалидный reply падает дальше по цепочке, @username
    резолвится."""
    real_id = 950110
    await _ensure_user(session, real_id, "Настоящий", username="real_target")
    await session.commit()

    reply_to = SimpleNamespace(
        from_user=_fake_user(TELEGRAM_SERVICE_ACCOUNT_ID, is_bot=False)
    )
    message = _fake_message(reply_to_message=reply_to)

    result = await target_resolution_service.resolve_target(message, session, "@real_target")

    assert result == (real_id, "Настоящий")


@pytest.mark.asyncio
async def test_resolve_target_falls_through_reply_to_bot_account(session):
    """reply на любой bot-аккаунт (is_bot=True), никакого другого способа
    резолва — итог None."""
    reply_to = SimpleNamespace(from_user=_fake_user(123456789, is_bot=True))
    message = _fake_message(reply_to_message=reply_to)

    result = await target_resolution_service.resolve_target(message, session, None)

    assert result is None


@pytest.mark.asyncio
async def test_resolve_target_falls_through_reply_to_bot_account_to_text_mention(session):
    """reply на bot-аккаунт падает дальше по цепочке — валидный text_mention
    в том же сообщении резолвится вместо него."""
    real_id = 950111
    reply_to = SimpleNamespace(from_user=_fake_user(123456790, is_bot=True))
    message = _fake_message(
        reply_to_message=reply_to,
        entities=[_entity("text_mention", user=_fake_user(real_id, is_bot=False, first_name="Упомянутый"))],
    )

    result = await target_resolution_service.resolve_target(message, session, None)

    assert result == (real_id, "Упомянутый")


# --- resolve_target: text_mention на служебный аккаунт -----------------------


@pytest.mark.asyncio
async def test_resolve_target_falls_through_text_mention_of_telegram_service_account_no_fallback(session):
    message = _fake_message(
        entities=[
            _entity(
                "text_mention",
                user=_fake_user(TELEGRAM_SERVICE_ACCOUNT_ID, is_bot=False, first_name="Telegram"),
            )
        ]
    )

    result = await target_resolution_service.resolve_target(message, session, None)

    assert result is None


@pytest.mark.asyncio
async def test_resolve_target_falls_through_text_mention_of_telegram_service_account_to_username_arg(session):
    real_id = 950112
    await _ensure_user(session, real_id, "Реальный3", username="real3")
    await session.commit()

    message = _fake_message(
        entities=[
            _entity(
                "text_mention",
                user=_fake_user(TELEGRAM_SERVICE_ACCOUNT_ID, is_bot=False, first_name="Telegram"),
            )
        ]
    )

    result = await target_resolution_service.resolve_target(message, session, "@real3")

    assert result == (real_id, "Реальный3")


# --- resolve_target: обычный валидный путь (регрессия) -----------------------


@pytest.mark.asyncio
async def test_resolve_target_resolves_valid_reply(session):
    real_id = 950120
    reply_to = SimpleNamespace(from_user=_fake_user(real_id, is_bot=False, first_name="Отвечено"))
    message = _fake_message(reply_to_message=reply_to)

    result = await target_resolution_service.resolve_target(message, session, None)

    assert result == (real_id, "Отвечено")


@pytest.mark.asyncio
async def test_resolve_target_resolves_valid_text_mention(session):
    real_id = 950121
    message = _fake_message(
        entities=[
            _entity("text_mention", user=_fake_user(real_id, is_bot=False, first_name="Упомянут"))
        ]
    )

    result = await target_resolution_service.resolve_target(message, session, None)

    assert result == (real_id, "Упомянут")


@pytest.mark.asyncio
async def test_resolve_target_resolves_valid_username_arg(session):
    real_id = 950122
    await _ensure_user(session, real_id, "ПоЮзернейму", username="valid_user")
    await session.commit()
    message = _fake_message()

    result = await target_resolution_service.resolve_target(message, session, "@valid_user")

    assert result == (real_id, "ПоЮзернейму")


@pytest.mark.asyncio
async def test_resolve_target_reply_takes_priority_over_text_mention_and_arg(session):
    """reply > text_mention entity > @username/id-аргумент — три
    одновременно валидных способа резолва, побеждает reply (документированный
    приоритет, не только "какой-то один сработавший")."""
    reply_id, mention_id, arg_id = 950123, 950124, 950125
    await _ensure_user(session, arg_id, "ПоАргументу", username="by_arg")
    await session.commit()

    reply_to = SimpleNamespace(from_user=_fake_user(reply_id, is_bot=False, first_name="ПоReply"))
    message = _fake_message(
        reply_to_message=reply_to,
        entities=[
            _entity("text_mention", user=_fake_user(mention_id, is_bot=False, first_name="ПоМеншену"))
        ],
    )

    result = await target_resolution_service.resolve_target(message, session, "@by_arg")

    assert result == (reply_id, "ПоReply")


@pytest.mark.asyncio
async def test_resolve_target_returns_none_when_no_reply_no_mention_no_arg(session):
    message = _fake_message()

    result = await target_resolution_service.resolve_target(message, session, None)

    assert result is None
