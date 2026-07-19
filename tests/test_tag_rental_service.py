"""Тесты tag_rental_service (TAG-02) — рынок аренды Telegram custom_title за
ювики (мокнутый `bot` из tests/conftest.py, живой Postgres через фикстуру
`session`, форма test_tag_service.py / test_duel_service.py).

Доказывают:
- _price(days) — settings.tag_rent_per_day * days (500/1500/3500 для 1/3/7).
- rent_title списывает цену через economy_service (баланс↓, банк↑,
  идентично debit_to_bank) и зовёт tag_service.grant_title(source='rental',
  expires_at≈now+days*24h) — active_titles-строка source='rental' status='active'.
- days вне settings.tag_rent_allowed_days отклоняется ДО списания/гранта.
- Свободный title длиннее settings.title_max отклоняется ДО списания/гранта
  (T-05-01 — делегируется tag_service.validate_title).
- Недостаток средств поднимает economy_service.InsufficientFunds, титул не выдан.
- Повтор rent_title с тем же ref_id (ретрай апдейта Telegram) не списывает
  дважды и не зовёт Bot API повторно — идемпотентный no-op, возвращает уже
  созданную ранее rental-строку.
- Приоритет номинанта (D-07): активная rental-строка, перекрытая грантом
  victim, подвешивается в 'suspended' и восстанавливается на экспайре victim
  (интеграция поверх реальной аренды, созданной rent_title).
- cancel_rental — status-переход в 'cancelled' (+ снятие реального тега, если
  была активна); повторный вызов — идемпотентный no-op (False).
"""

from __future__ import annotations

from datetime import datetime
from datetime import timedelta

import pytest
from sqlalchemy import select

from bot.config import settings
from bot.services import economy_service
from bot.services import tag_rental_service
from bot.services import tag_service
from common.models.active_title import ActiveTitle
from common.models.user import User
from common.models.user_balance import UserBalance
from common.models.chat_bank import ChatBank


# --- Хелперы (форма test_duel_service.py / test_tag_service.py) -------------


async def _ensure_user(session, user_id: int, first_name: str = "Тест") -> None:
    session.add(User(id=user_id, first_name=first_name))
    await session.flush()


async def _fund(session, chat_id: int, user_id: int) -> int:
    """Заводит кошелёк (стартовый бонус economy_start_bonus) и коммитит."""
    return await economy_service.get_balance(session, chat_id, user_id)


async def _topup(session, chat_id: int, user_id: int, amount: int, ref_id: str) -> None:
    """Довносит баланс сверх стартового бонуса — 3/7-дневная аренда (1500/3500)
    дороже settings.economy_start_bonus (1000 по умолчанию), тестам нужен
    запас, не связанный с самим TAG-02 сценарием недостатка средств."""
    await economy_service.credit(session, chat_id, user_id, amount, kind="test_topup", ref_id=ref_id)
    await session.commit()


async def _get_user_balance(session, chat_id: int, user_id: int) -> int:
    result = await session.execute(
        select(UserBalance.balance).where(
            UserBalance.chat_id == chat_id, UserBalance.user_id == user_id
        )
    )
    return result.scalar_one()


async def _get_bank_balance(session, chat_id: int) -> int:
    result = await session.execute(select(ChatBank.balance).where(ChatBank.chat_id == chat_id))
    return result.scalar_one_or_none() or 0


async def _get_active_title(session, chat_id: int, user_id: int, source: str) -> ActiveTitle | None:
    result = await session.execute(
        select(ActiveTitle)
        .where(
            ActiveTitle.chat_id == chat_id,
            ActiveTitle.user_id == user_id,
            ActiveTitle.source == source,
        )
        .order_by(ActiveTitle.id.desc())
    )
    return result.scalars().first()


# --- _price / _allowed_days ---------------------------------------------------


def test_rent_price_by_days():
    assert tag_rental_service._price(1) == settings.tag_rent_per_day
    assert tag_rental_service._price(3) == settings.tag_rent_per_day * 3
    assert tag_rental_service._price(7) == settings.tag_rent_per_day * 7


# --- rent_title: оплата + grant (happy path) ----------------------------------


@pytest.mark.asyncio
async def test_rent_charges_and_grants(session, bot):
    chat_id = -1009007001
    user_id = 9007001
    await _ensure_user(session, user_id, "Арендатор")
    await _fund(session, chat_id, user_id)
    await _topup(session, chat_id, user_id, 5000, "test_topup_1")
    balance_before = await _get_user_balance(session, chat_id, user_id)
    bank_before = await _get_bank_balance(session, chat_id)

    row = await tag_rental_service.rent_title(
        session, chat_id, user_id, "Босс", 3, "tag_rent:test:1", bot
    )
    await session.commit()

    price = settings.tag_rent_per_day * 3
    assert row.status == "active"
    assert row.source == "rental"
    assert row.title == "Босс"
    assert row.price_paid == price

    assert await _get_user_balance(session, chat_id, user_id) == balance_before - price
    assert await _get_bank_balance(session, chat_id) == bank_before + price

    bot.promote_chat_member.assert_awaited_once()
    bot.set_chat_administrator_custom_title.assert_awaited_once()

    expected_expiry = datetime.utcnow() + timedelta(days=3)
    assert abs((row.expires_at - expected_expiry).total_seconds()) < 5


# --- rent_title: days вне allowed_days отклоняется ДО списания ---------------


@pytest.mark.asyncio
async def test_rent_rejects_bad_days(session, bot):
    chat_id = -1009007002
    user_id = 9007002
    await _ensure_user(session, user_id)
    balance_before = await _fund(session, chat_id, user_id)

    with pytest.raises(tag_rental_service.TagRentalError):
        await tag_rental_service.rent_title(
            session, chat_id, user_id, "Титул", 2, "tag_rent:test:2", bot
        )

    assert await _get_user_balance(session, chat_id, user_id) == balance_before
    bot.promote_chat_member.assert_not_awaited()


# --- rent_title: title длиннее title_max отклоняется ДО списания (T-05-01) ---


@pytest.mark.asyncio
async def test_rent_rejects_long_title(session, bot):
    chat_id = -1009007003
    user_id = 9007003
    await _ensure_user(session, user_id)
    balance_before = await _fund(session, chat_id, user_id)

    long_title = "А" * (settings.title_max + 1)
    with pytest.raises(tag_service.TagError):
        await tag_rental_service.rent_title(
            session, chat_id, user_id, long_title, 1, "tag_rent:test:3", bot
        )

    assert await _get_user_balance(session, chat_id, user_id) == balance_before
    bot.promote_chat_member.assert_not_awaited()


# --- rent_title: недостаток средств -------------------------------------------


@pytest.mark.asyncio
async def test_rent_insufficient_funds(session, bot):
    chat_id = -1009007004
    user_id = 9007004
    await _ensure_user(session, user_id)
    await _fund(session, chat_id, user_id)
    # Обнуляем баланс, чтобы гарантированно не хватило на аренду.
    await economy_service.debit(
        session,
        chat_id,
        user_id,
        settings.economy_start_bonus,
        kind="test_drain",
        ref_id="test_drain_tag_rent_4",
    )
    await session.commit()

    with pytest.raises(economy_service.InsufficientFunds):
        await tag_rental_service.rent_title(
            session, chat_id, user_id, "Титул", 1, "tag_rent:test:4", bot
        )

    assert await _get_active_title(session, chat_id, user_id, "rental") is None
    bot.promote_chat_member.assert_not_awaited()


# --- rent_title: идемпотентность повтора ref_id -------------------------------


@pytest.mark.asyncio
async def test_rent_idempotent_on_retry(session, bot):
    chat_id = -1009007005
    user_id = 9007005
    await _ensure_user(session, user_id)
    balance_before = await _fund(session, chat_id, user_id)

    ref_id = "tag_rent:test:5"
    first = await tag_rental_service.rent_title(
        session, chat_id, user_id, "Титул", 1, ref_id, bot
    )
    await session.commit()

    price = settings.tag_rent_per_day
    balance_after_first = await _get_user_balance(session, chat_id, user_id)
    assert balance_after_first == balance_before - price

    bot.reset_mock()
    second = await tag_rental_service.rent_title(
        session, chat_id, user_id, "Титул", 1, ref_id, bot
    )
    await session.commit()

    assert second.id == first.id
    assert await _get_user_balance(session, chat_id, user_id) == balance_after_first
    bot.promote_chat_member.assert_not_awaited()
    bot.set_chat_administrator_custom_title.assert_not_awaited()


# --- WR-01 (05-REVIEW.md): ретрай возвращает ИМЕННО свою строку, не самую свежую --


@pytest.mark.asyncio
async def test_rent_idempotent_retry_returns_own_row_not_latest(session, bot):
    """Юзер арендовал title A (ref_id=1), затем арендовал ДРУГОЙ title B
    (ref_id=2, отдельное сообщение) — теперь у него две rental-строки.
    Реплей ref_id=1 (ретрай апдейта Telegram на самое первое сообщение)
    должен вернуть строку A (title/price/expires_at ПЕРВОЙ аренды), а не
    "самую свежую" строку B — иначе хендлер покажет юзеру чужие данные в
    подтверждении (bot/handlers/tags.py f"Тег «{row.title}» ...")."""
    chat_id = -1009007008
    user_id = 9007008
    await _ensure_user(session, user_id)
    await _fund(session, chat_id, user_id)
    await _topup(session, chat_id, user_id, 5000, "test_topup_8")

    first = await tag_rental_service.rent_title(
        session, chat_id, user_id, "Первый", 1, "tag_rent:test:8a", bot
    )
    await session.commit()
    bot.reset_mock()

    # Первая аренда 1-дневная, expires_at в прошлом почти сразу — экспайрим
    # её вручную, чтобы вторая аренда могла стать активной (иначе grant_title
    # просто подвесит вторую rental-строку, что не мешает тесту, но нагляднее
    # проверить на двух ПОЛНОЦЕННЫХ rental-строках).
    second = await tag_rental_service.rent_title(
        session, chat_id, user_id, "Второй", 3, "tag_rent:test:8b", bot
    )
    await session.commit()

    assert second.id != first.id

    # Реплей САМОГО ПЕРВОГО ref_id (debit_to_bank вернёт charged=False).
    replay = await tag_rental_service.rent_title(
        session, chat_id, user_id, "Первый", 1, "tag_rent:test:8a", bot
    )
    await session.commit()

    assert replay.id == first.id
    assert replay.title == "Первый"
    assert replay.price_paid == first.price_paid


# --- Приоритет номинанта поверх реальной аренды (D-07) -------------------------


@pytest.mark.asyncio
async def test_nomination_suspends_then_restores_rental(session, bot):
    chat_id = -1009007006
    user_id = 9007006
    await _ensure_user(session, user_id)
    await _fund(session, chat_id, user_id)
    await _topup(session, chat_id, user_id, 5000, "test_topup_6")

    rental = await tag_rental_service.rent_title(
        session, chat_id, user_id, "Аренда", 3, "tag_rent:test:6", bot
    )
    await session.commit()
    bot.reset_mock()

    victim_expires = datetime.utcnow() - timedelta(hours=1)  # уже просрочен
    victim_row = await tag_service.grant_title(
        bot, session, chat_id, user_id, "Жертва", "victim", victim_expires
    )
    await session.commit()

    await session.refresh(rental)
    assert rental.status == "suspended"

    bot.reset_mock()
    processed = await tag_service.expire_due(bot, session)
    await session.commit()

    assert processed == 1
    await session.refresh(rental)
    await session.refresh(victim_row)
    assert victim_row.status == "expired"
    assert rental.status == "active"


# --- cancel_rental: идемпотентность -------------------------------------------


@pytest.mark.asyncio
async def test_cancel_idempotent(session, bot):
    chat_id = -1009007007
    user_id = 9007007
    await _ensure_user(session, user_id)
    await _fund(session, chat_id, user_id)

    await tag_rental_service.rent_title(
        session, chat_id, user_id, "Аренда", 1, "tag_rent:test:7", bot
    )
    await session.commit()
    bot.reset_mock()

    cancelled = await tag_rental_service.cancel_rental(session, chat_id, user_id, bot)
    await session.commit()
    assert cancelled is True

    stored = await _get_active_title(session, chat_id, user_id, "rental")
    assert stored is not None
    assert stored.status == "cancelled"
    bot.promote_chat_member.assert_awaited()  # демот (снятие тега)

    bot.reset_mock()
    cancelled_again = await tag_rental_service.cancel_rental(session, chat_id, user_id, bot)
    await session.commit()
    assert cancelled_again is False
    bot.promote_chat_member.assert_not_awaited()
