"""Тесты tag_service (TAG-01) — единственный владелец Telegram member-тега и
таблицы active_titles (мокнутый `bot` из tests/conftest.py, живой Postgres
через фикстуру `session`, форма test_duel_service.py).

Доказывают:
- grant_title зовёт bot.set_chat_member_tag(tag=title) — прямой API для
  обычных участников (Bot API 2026-03, `can_manage_tags`), без промоута в
  админы (см. ревизию 2026-07-23 в docstring tag_service.py).
- validate_title отклоняет title длиннее settings.title_max ДО любого
  обращения к Bot API (T-05-01) — set_chat_member_tag не вызывается.
- active_titles — единственный владелец Telegram-эффекта: grant_title
  (source='victim') подвешивает активную 'rental'-строку того же юзера в
  'suspended' (не удаляет её) — приоритет номинанта над арендатором (D-07/D-10).
- expire_due снимает тег с просроченных active-титулов (status->expired) и
  ВОССТАНАВЛИВАЕТ подвешенную аренду того же юзера (suspended->active,
  повторный set_chat_member_tag — тег не переживает снятие сам по себе).
- Сбой Telegram API (TelegramBadRequest) ловится defensively в grant_title —
  та же дисциплина, что мут дуэли (test_duel_accept_handler_survives_mute_failure).
"""

from __future__ import annotations

from datetime import datetime
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
from aiogram.exceptions import TelegramBadRequest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from bot.config import settings
from bot.services import tag_service
from common.models.active_title import ActiveTitle
from common.models.user import User


# --- Хелперы (форма test_duel_service.py) ------------------------------------


async def _ensure_user(session, user_id: int, first_name: str = "Тест") -> None:
    session.add(User(id=user_id, first_name=first_name))
    await session.flush()


async def _get_active_title(session, chat_id: int, user_id: int, source: str) -> ActiveTitle:
    result = await session.execute(
        select(ActiveTitle).where(
            ActiveTitle.chat_id == chat_id,
            ActiveTitle.user_id == user_id,
            ActiveTitle.source == source,
        )
    )
    return result.scalars().first()


# --- grant_title: прямой set_chat_member_tag, без промоута -------------------


@pytest.mark.asyncio
async def test_grant_sets_member_tag(session, bot):
    chat_id = -1009006001
    user_id = 9006001
    await _ensure_user(session, user_id, "Номинант")

    expires_at = datetime.utcnow() + timedelta(hours=24)
    row = await tag_service.grant_title(
        bot, session, chat_id, user_id, "Жертва дня", "victim", expires_at
    )
    await session.commit()

    bot.set_chat_member_tag.assert_awaited_once_with(
        chat_id=chat_id, user_id=user_id, tag="Жертва дня"
    )

    assert row.status == "active"
    assert row.source == "victim"
    assert row.title == "Жертва дня"

    stored = await _get_active_title(session, chat_id, user_id, "victim")
    assert stored is not None
    assert stored.status == "active"


# --- validate_title: длина ДО обращения к Bot API (T-05-01) ----------------


@pytest.mark.asyncio
async def test_grant_rejects_long_title(session, bot):
    chat_id = -1009006002
    user_id = 9006002
    await _ensure_user(session, user_id)

    long_title = "А" * (settings.title_max + 1)
    with pytest.raises(tag_service.TagError):
        await tag_service.grant_title(
            bot,
            session,
            chat_id,
            user_id,
            long_title,
            "victim",
            datetime.utcnow() + timedelta(hours=24),
        )

    bot.set_chat_member_tag.assert_not_awaited()


# --- active_titles state machine: victim подвешивает active rental (D-07) ---


@pytest.mark.asyncio
async def test_grant_victim_suspends_active_rental(session, bot):
    chat_id = -1009006003
    user_id = 9006003
    await _ensure_user(session, user_id)

    rental_expires = datetime.utcnow() + timedelta(days=3)
    rental_row = await tag_service.grant_title(
        bot, session, chat_id, user_id, "Аренда", "rental", rental_expires
    )
    await session.commit()
    bot.reset_mock()

    victim_expires = datetime.utcnow() + timedelta(hours=24)
    victim_row = await tag_service.grant_title(
        bot, session, chat_id, user_id, "Жертва", "victim", victim_expires
    )
    await session.commit()

    await session.refresh(rental_row)
    await session.refresh(victim_row)
    assert rental_row.status == "suspended"
    assert victim_row.status == "active"
    assert victim_row.source == "victim"


# --- expire_due: снятие тега с просроченного + восстановление аренды --------


@pytest.mark.asyncio
async def test_expire_demotes_and_restores_rental(session, bot):
    chat_id = -1009006004
    user_id = 9006004
    await _ensure_user(session, user_id)

    rental_expires = datetime.utcnow() + timedelta(days=3)
    rental_row = await tag_service.grant_title(
        bot, session, chat_id, user_id, "Аренда", "rental", rental_expires
    )
    await session.commit()

    victim_expires = datetime.utcnow() - timedelta(hours=1)  # уже просрочен
    victim_row = await tag_service.grant_title(
        bot, session, chat_id, user_id, "Жертва", "victim", victim_expires
    )
    await session.commit()

    await session.refresh(rental_row)
    assert rental_row.status == "suspended"

    bot.reset_mock()
    processed = await tag_service.expire_due(bot, session)
    await session.commit()

    assert processed == 1

    await session.refresh(victim_row)
    await session.refresh(rental_row)
    assert victim_row.status == "expired"
    assert rental_row.status == "active"

    # Тег не переживает снятие сам по себе — восстановленная аренда получает
    # повторный set_chat_member_tag со своим сохранённым title.
    tag_calls = [
        c
        for c in bot.set_chat_member_tag.await_args_list
        if c.kwargs.get("tag") == "Аренда"
    ]
    assert tag_calls

    # Просроченный victim-тег снимается явным tag=None.
    clear_calls = [
        c
        for c in bot.set_chat_member_tag.await_args_list
        if c.kwargs.get("user_id") == user_id and c.kwargs.get("tag") is None
    ]
    assert clear_calls


# --- CR-01 (05-REVIEW.md): гонка "existing=None, но INSERT конфликтует" -----


@pytest.mark.asyncio
async def test_grant_retries_once_on_integrity_error(session, bot):
    """Симулирует ровно ту гонку, что описана в CR-01: на момент SELECT ...
    FOR UPDATE активной строки ещё не было (или она "потерялась" из
    результата после разблокировки — см. docstring grant_title), но флаш
    INSERT'а конфликтует с частичным UNIQUE(chat_id, user_id) WHERE
    status='active', который в реальности только что зафиксировала
    выигравшая гонку транзакция. Здесь конфликт при первом флаше
    симулируется напрямую (без второй живой транзакции) — доказывает, что
    grant_title перечитывает состояние и повторяет ОДИН раз вместо того,
    чтобы пробросить IntegrityError наружу (это именно то, что раньше падало
    необработанным исключением — bot/handlers/tags.py не ловит
    IntegrityError вовсе, см. Impact в CR-01)."""
    chat_id = -1009006006
    user_id = 9006006
    await _ensure_user(session, user_id)

    real_flush = session.flush
    calls = {"n": 0}

    async def flaky_flush(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise IntegrityError(
                "INSERT INTO active_titles ...",
                {},
                Exception('duplicate key value violates unique constraint "uq_active_title_user_active"'),
            )
        return await real_flush(*args, **kwargs)

    session.flush = flaky_flush
    try:
        expires_at = datetime.utcnow() + timedelta(hours=24)
        row = await tag_service.grant_title(
            bot, session, chat_id, user_id, "Ретрай", "victim", expires_at
        )
    finally:
        session.flush = real_flush
    await session.commit()

    # IntegrityError не пробрасывается наружу — flush был вызван дважды
    # (первый упал, второй — реальный, успешный).
    assert calls["n"] == 2
    assert row.status == "active"

    stored = await _get_active_title(session, chat_id, user_id, "victim")
    assert stored is not None
    assert stored.status == "active"
    assert stored.id == row.id

    # Успешно выданный тег ПОСЛЕ повтора — Bot API всё равно вызывается.
    bot.set_chat_member_tag.assert_awaited_once_with(
        chat_id=chat_id, user_id=user_id, tag="Ретрай"
    )


# --- Сбой Telegram API не должен ронять флоу (defensive-catch) --------------


@pytest.mark.asyncio
async def test_grant_survives_telegram_error(session, bot):
    chat_id = -1009006005
    user_id = 9006005
    await _ensure_user(session, user_id)

    bot.set_chat_member_tag.side_effect = TelegramBadRequest(
        method=AsyncMock(), message="CHAT_ADMIN_REQUIRED"
    )

    expires_at = datetime.utcnow() + timedelta(hours=24)
    row = await tag_service.grant_title(
        bot, session, chat_id, user_id, "Жертва", "victim", expires_at
    )
    await session.commit()

    assert row.status == "active"
    stored = await _get_active_title(session, chat_id, user_id, "victim")
    assert stored is not None
    assert stored.status == "active"
