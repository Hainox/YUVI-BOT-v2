"""Интеграционные тесты exchange_service (EXCHANGE-01) против живого Postgres
(фикстура `session` из tests/conftest.py). Доказывают:

- create_listing эскроирует ювики продавца через economy_service.debit,
  НИКОГДА не заходит в chat_bank (форма duel_service WR-04 теста
  test_create_escrows_challenger_stake).
- create_listing валидирует сумму (D-04-порог casino_min_bet) и
  want_description (непусто, <= 300 симв.) ДО любого движения денег.
- create_listing идемпотентен на повторе ref_id (ListingAlreadyResolved,
  деньги не списываются дважды).
- claim_listing — self-trade guard (продавец не может заклеймить свой же
  листинг), гоночная защита (второй claim на уже claimed — no-op), деньги
  НЕ двигаются при claim.
- cancel_listing — только продавец, только пока open, полный рефанд,
  статус-переход как гард идемпотентности (форма
  markets_service.cancel_market).
- confirm_fulfillment — только продавец, только пока claimed, полный релиз
  эскроу заклеймившему покупателю, идемпотентен.
- admin_force_cancel/admin_force_release — не гейтятся актором внутри
  сервиса (гейтинг — обязанность вызывающего, см. bot/handlers/exchange.py),
  работают на open+claimed/только claimed соответственно, идемпотентны.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from bot.config import settings
from bot.services import economy_service
from bot.services import exchange_service
from common.models.chat_bank import ChatBank
from common.models.exchange_listing import ExchangeListing
from common.models.user import User
from common.models.user_balance import UserBalance


# --- Хелперы (форма tests/test_duel_service.py) ------------------------------


async def _ensure_user(session, user_id: int, first_name: str = "Тест") -> None:
    session.add(User(id=user_id, first_name=first_name))
    await session.flush()


async def _fund(session, chat_id: int, user_id: int) -> int:
    """Заводит кошелёк (стартовый бонус economy_start_bonus) и коммитит."""
    return await economy_service.get_balance(session, chat_id, user_id)


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


async def _get_listing(session, listing_id: int) -> ExchangeListing:
    return (
        await session.execute(select(ExchangeListing).where(ExchangeListing.id == listing_id))
    ).scalar_one()


# --- create_listing (эскроу продавца) ----------------------------------------


@pytest.mark.asyncio
async def test_create_escrows_seller_amount_and_never_touches_bank(session):
    chat_id = -100920001
    seller_id = 920001
    await _ensure_user(session, seller_id, "Продавец")
    seller_before = await _fund(session, chat_id, seller_id)
    bank_before = await _get_bank_balance(session, chat_id)

    amount = 100
    listing = await exchange_service.create_listing(
        session, chat_id, seller_id, amount, "10 ювиков за подписку на канал", "test_create_escrow"
    )

    assert listing.status == "open"
    assert listing.seller_user_id == seller_id
    assert listing.yuvik_amount == amount
    assert listing.want_description == "10 ювиков за подписку на канал"
    assert listing.claimed_by_user_id is None

    assert await _get_user_balance(session, chat_id, seller_id) == seller_before - amount
    # WR (форма duel_service WR-04): эскроу не заходит в общий chat_bank.
    assert await _get_bank_balance(session, chat_id) == bank_before


@pytest.mark.asyncio
async def test_create_below_min_amount_raises_and_does_not_escrow(session):
    chat_id = -100920002
    seller_id = 920002
    await _ensure_user(session, seller_id)
    balance_before = await _fund(session, chat_id, seller_id)

    with pytest.raises(exchange_service.ExchangeError):
        await exchange_service.create_listing(
            session,
            chat_id,
            seller_id,
            settings.casino_min_bet - 1,
            "что-то",
            "test_create_below_min",
        )

    assert await _get_user_balance(session, chat_id, seller_id) == balance_before
    listings = (
        await session.execute(select(ExchangeListing).where(ExchangeListing.seller_user_id == seller_id))
    ).scalars().all()
    assert listings == []


@pytest.mark.asyncio
async def test_create_empty_want_description_raises_and_does_not_escrow(session):
    chat_id = -100920003
    seller_id = 920003
    await _ensure_user(session, seller_id)
    balance_before = await _fund(session, chat_id, seller_id)

    with pytest.raises(exchange_service.ExchangeError):
        await exchange_service.create_listing(
            session, chat_id, seller_id, 100, "   ", "test_create_empty_desc"
        )

    assert await _get_user_balance(session, chat_id, seller_id) == balance_before


@pytest.mark.asyncio
async def test_create_too_long_want_description_raises(session):
    chat_id = -100920004
    seller_id = 920004
    await _ensure_user(session, seller_id)
    balance_before = await _fund(session, chat_id, seller_id)

    with pytest.raises(exchange_service.ExchangeError):
        await exchange_service.create_listing(
            session, chat_id, seller_id, 100, "x" * 301, "test_create_too_long_desc"
        )

    assert await _get_user_balance(session, chat_id, seller_id) == balance_before


@pytest.mark.asyncio
async def test_create_idempotent_on_replayed_ref_id(session):
    chat_id = -100920005
    seller_id = 920005
    await _ensure_user(session, seller_id)
    seller_before = await _fund(session, chat_id, seller_id)

    ref_id = "test_create_replay"
    await exchange_service.create_listing(session, chat_id, seller_id, 100, "что-то", ref_id)
    balance_after_first = await _get_user_balance(session, chat_id, seller_id)

    with pytest.raises(exchange_service.ListingAlreadyResolved):
        await exchange_service.create_listing(session, chat_id, seller_id, 100, "что-то другое", ref_id)

    # деньги не списаны повторно, второй листинг не создан
    assert await _get_user_balance(session, chat_id, seller_id) == balance_after_first
    assert balance_after_first == seller_before - 100
    listings = (
        await session.execute(select(ExchangeListing).where(ExchangeListing.seller_user_id == seller_id))
    ).scalars().all()
    assert len(listings) == 1


@pytest.mark.asyncio
async def test_create_rejects_non_yuvik_item_type(session):
    chat_id = -100920006
    seller_id = 920006
    await _ensure_user(session, seller_id)
    balance_before = await _fund(session, chat_id, seller_id)

    with pytest.raises(exchange_service.ExchangeError):
        await exchange_service.create_listing(
            session,
            chat_id,
            seller_id,
            100,
            "карту дай",
            "test_create_gacha_stub",
            item_type="gacha_card",
            gacha_char_id="some_char",
        )

    assert await _get_user_balance(session, chat_id, seller_id) == balance_before


# --- claim_listing (мягкий сигнал, деньги не двигаются) ---------------------


@pytest.mark.asyncio
async def test_claim_self_trade_guard(session):
    chat_id = -100920007
    seller_id = 920007
    await _ensure_user(session, seller_id)
    await _fund(session, chat_id, seller_id)

    listing = await exchange_service.create_listing(
        session, chat_id, seller_id, 100, "что-то", "test_claim_self_create"
    )

    with pytest.raises(exchange_service.ExchangeError):
        await exchange_service.claim_listing(session, chat_id, listing.id, seller_id)

    listing_row = await _get_listing(session, listing.id)
    assert listing_row.status == "open"
    assert listing_row.claimed_by_user_id is None


@pytest.mark.asyncio
async def test_claim_transitions_to_claimed_without_moving_money(session):
    chat_id = -100920008
    seller_id, buyer_id = 920008, 920009
    await _ensure_user(session, seller_id)
    await _ensure_user(session, buyer_id)
    await _fund(session, chat_id, seller_id)
    buyer_before = await _fund(session, chat_id, buyer_id)

    listing = await exchange_service.create_listing(
        session, chat_id, seller_id, 100, "что-то", "test_claim_create"
    )

    result = await exchange_service.claim_listing(session, chat_id, listing.id, buyer_id)

    assert result["status"] == "claimed"
    assert result["claimed"] is True
    assert result["claimed_by_user_id"] == buyer_id

    listing_row = await _get_listing(session, listing.id)
    assert listing_row.status == "claimed"
    assert listing_row.claimed_by_user_id == buyer_id
    assert listing_row.claimed_at is not None

    # claim — не платёж, баланс покупателя не меняется.
    assert await _get_user_balance(session, chat_id, buyer_id) == buyer_before


@pytest.mark.asyncio
async def test_claim_race_on_already_claimed_is_noop(session):
    chat_id = -100920010
    seller_id, buyer_id, latecomer_id = 920010, 920011, 920012
    await _ensure_user(session, seller_id)
    await _ensure_user(session, buyer_id)
    await _ensure_user(session, latecomer_id)
    await _fund(session, chat_id, seller_id)
    await _fund(session, chat_id, buyer_id)
    await _fund(session, chat_id, latecomer_id)

    listing = await exchange_service.create_listing(
        session, chat_id, seller_id, 100, "что-то", "test_claim_race_create"
    )

    first = await exchange_service.claim_listing(session, chat_id, listing.id, buyer_id)
    second = await exchange_service.claim_listing(session, chat_id, listing.id, latecomer_id)

    assert first["claimed"] is True
    assert second["claimed"] is False
    assert second["status"] == "claimed"

    listing_row = await _get_listing(session, listing.id)
    assert listing_row.claimed_by_user_id == buyer_id


@pytest.mark.asyncio
async def test_claim_not_found_raises(session):
    chat_id = -100920013
    buyer_id = 920014
    await _ensure_user(session, buyer_id)
    await _fund(session, chat_id, buyer_id)

    with pytest.raises(exchange_service.ListingNotFound):
        await exchange_service.claim_listing(session, chat_id, 999999999, buyer_id)


# --- cancel_listing (только продавец, только пока open, полный рефанд) -----


@pytest.mark.asyncio
async def test_cancel_refunds_seller_in_full(session):
    chat_id = -100920015
    seller_id = 920015
    await _ensure_user(session, seller_id)
    seller_before = await _fund(session, chat_id, seller_id)

    listing = await exchange_service.create_listing(
        session, chat_id, seller_id, 100, "что-то", "test_cancel_create"
    )

    result = await exchange_service.cancel_listing(session, chat_id, listing.id, seller_id)

    assert result["status"] == "cancelled"
    assert result["refunded"] == 100
    listing_row = await _get_listing(session, listing.id)
    assert listing_row.status == "cancelled"
    assert listing_row.resolved_at is not None
    assert await _get_user_balance(session, chat_id, seller_id) == seller_before


@pytest.mark.asyncio
async def test_cancel_by_non_seller_raises(session):
    chat_id = -100920016
    seller_id, stranger_id = 920016, 920017
    await _ensure_user(session, seller_id)
    await _ensure_user(session, stranger_id)
    await _fund(session, chat_id, seller_id)
    await _fund(session, chat_id, stranger_id)

    listing = await exchange_service.create_listing(
        session, chat_id, seller_id, 100, "что-то", "test_cancel_stranger_create"
    )

    with pytest.raises(exchange_service.ExchangeError):
        await exchange_service.cancel_listing(session, chat_id, listing.id, stranger_id)

    listing_row = await _get_listing(session, listing.id)
    assert listing_row.status == "open"


@pytest.mark.asyncio
async def test_cancel_once_claimed_is_noop_no_refund(session):
    chat_id = -100920018
    seller_id, buyer_id = 920018, 920019
    await _ensure_user(session, seller_id)
    await _ensure_user(session, buyer_id)
    seller_before_escrow = await _fund(session, chat_id, seller_id)
    await _fund(session, chat_id, buyer_id)

    listing = await exchange_service.create_listing(
        session, chat_id, seller_id, 100, "что-то", "test_cancel_claimed_create"
    )
    await exchange_service.claim_listing(session, chat_id, listing.id, buyer_id)

    result = await exchange_service.cancel_listing(session, chat_id, listing.id, seller_id)

    assert result["status"] == "claimed"
    assert result["refunded"] == 0
    listing_row = await _get_listing(session, listing.id)
    assert listing_row.status == "claimed"
    # ювики остаются в эскроу (списаны на создании, но не возвращены).
    assert await _get_user_balance(session, chat_id, seller_id) == seller_before_escrow - 100


@pytest.mark.asyncio
async def test_cancel_idempotent_second_call_is_noop(session):
    chat_id = -100920020
    seller_id = 920020
    await _ensure_user(session, seller_id)
    seller_before = await _fund(session, chat_id, seller_id)

    listing = await exchange_service.create_listing(
        session, chat_id, seller_id, 100, "что-то", "test_cancel_twice_create"
    )

    first = await exchange_service.cancel_listing(session, chat_id, listing.id, seller_id)
    second = await exchange_service.cancel_listing(session, chat_id, listing.id, seller_id)

    assert first["refunded"] == 100
    assert second["refunded"] == 0
    assert second["status"] == "cancelled"
    # рефанд не задвоился.
    assert await _get_user_balance(session, chat_id, seller_id) == seller_before


# --- confirm_fulfillment (только продавец, только пока claimed) -------------


@pytest.mark.asyncio
async def test_confirm_releases_full_escrow_to_buyer(session):
    chat_id = -100920021
    seller_id, buyer_id = 920021, 920022
    await _ensure_user(session, seller_id)
    await _ensure_user(session, buyer_id)
    await _fund(session, chat_id, seller_id)
    buyer_before = await _fund(session, chat_id, buyer_id)

    listing = await exchange_service.create_listing(
        session, chat_id, seller_id, 100, "что-то", "test_confirm_create"
    )
    await exchange_service.claim_listing(session, chat_id, listing.id, buyer_id)

    result = await exchange_service.confirm_fulfillment(
        session, chat_id, listing.id, seller_id, "test_confirm_ref"
    )

    assert result["status"] == "fulfilled"
    assert result["released"] == 100
    assert result["claimed_by_user_id"] == buyer_id

    listing_row = await _get_listing(session, listing.id)
    assert listing_row.status == "fulfilled"
    assert listing_row.resolved_at is not None
    assert await _get_user_balance(session, chat_id, buyer_id) == buyer_before + 100


@pytest.mark.asyncio
async def test_confirm_by_non_seller_raises(session):
    chat_id = -100920023
    seller_id, buyer_id = 920023, 920024
    await _ensure_user(session, seller_id)
    await _ensure_user(session, buyer_id)
    await _fund(session, chat_id, seller_id)
    await _fund(session, chat_id, buyer_id)

    listing = await exchange_service.create_listing(
        session, chat_id, seller_id, 100, "что-то", "test_confirm_stranger_create"
    )
    await exchange_service.claim_listing(session, chat_id, listing.id, buyer_id)

    with pytest.raises(exchange_service.ExchangeError):
        await exchange_service.confirm_fulfillment(session, chat_id, listing.id, buyer_id, "test_confirm_stranger_ref")

    listing_row = await _get_listing(session, listing.id)
    assert listing_row.status == "claimed"


@pytest.mark.asyncio
async def test_confirm_before_claim_is_noop(session):
    chat_id = -100920025
    seller_id = 920025
    await _ensure_user(session, seller_id)
    await _fund(session, chat_id, seller_id)

    listing = await exchange_service.create_listing(
        session, chat_id, seller_id, 100, "что-то", "test_confirm_no_claim_create"
    )

    result = await exchange_service.confirm_fulfillment(
        session, chat_id, listing.id, seller_id, "test_confirm_no_claim_ref"
    )

    assert result["status"] == "open"
    assert result["released"] == 0
    listing_row = await _get_listing(session, listing.id)
    assert listing_row.status == "open"


@pytest.mark.asyncio
async def test_confirm_idempotent_second_call_does_not_double_release(session):
    chat_id = -100920026
    seller_id, buyer_id = 920026, 920027
    await _ensure_user(session, seller_id)
    await _ensure_user(session, buyer_id)
    await _fund(session, chat_id, seller_id)
    buyer_before = await _fund(session, chat_id, buyer_id)

    listing = await exchange_service.create_listing(
        session, chat_id, seller_id, 100, "что-то", "test_confirm_twice_create"
    )
    await exchange_service.claim_listing(session, chat_id, listing.id, buyer_id)

    first = await exchange_service.confirm_fulfillment(
        session, chat_id, listing.id, seller_id, "test_confirm_twice_ref_1"
    )
    second = await exchange_service.confirm_fulfillment(
        session, chat_id, listing.id, seller_id, "test_confirm_twice_ref_2"
    )

    assert first["released"] == 100
    assert second["released"] == 0
    assert second["status"] == "fulfilled"
    assert await _get_user_balance(session, chat_id, buyer_id) == buyer_before + 100


# --- admin_force_cancel / admin_force_release (диспуты) ----------------------


@pytest.mark.asyncio
async def test_admin_force_cancel_refunds_open_listing(session):
    chat_id = -100920028
    seller_id = 920028
    await _ensure_user(session, seller_id)
    seller_before = await _fund(session, chat_id, seller_id)

    listing = await exchange_service.create_listing(
        session, chat_id, seller_id, 100, "что-то", "test_admin_cancel_open_create"
    )

    result = await exchange_service.admin_force_cancel(session, chat_id, listing.id)

    assert result["status"] == "cancelled"
    assert result["refunded"] == 100
    assert await _get_user_balance(session, chat_id, seller_id) == seller_before


@pytest.mark.asyncio
async def test_admin_force_cancel_refunds_claimed_listing(session):
    chat_id = -100920029
    seller_id, buyer_id = 920029, 920030
    await _ensure_user(session, seller_id)
    await _ensure_user(session, buyer_id)
    seller_before = await _fund(session, chat_id, seller_id)
    buyer_before = await _fund(session, chat_id, buyer_id)

    listing = await exchange_service.create_listing(
        session, chat_id, seller_id, 100, "что-то", "test_admin_cancel_claimed_create"
    )
    await exchange_service.claim_listing(session, chat_id, listing.id, buyer_id)

    result = await exchange_service.admin_force_cancel(session, chat_id, listing.id)

    assert result["status"] == "cancelled"
    assert result["refunded"] == 100
    assert await _get_user_balance(session, chat_id, seller_id) == seller_before
    # покупатель ничего не получает — спор разрешён в пользу продавца.
    assert await _get_user_balance(session, chat_id, buyer_id) == buyer_before


@pytest.mark.asyncio
async def test_admin_force_cancel_idempotent_on_terminal_status(session):
    chat_id = -100920031
    seller_id = 920031
    await _ensure_user(session, seller_id)
    seller_before = await _fund(session, chat_id, seller_id)

    listing = await exchange_service.create_listing(
        session, chat_id, seller_id, 100, "что-то", "test_admin_cancel_twice_create"
    )
    await exchange_service.cancel_listing(session, chat_id, listing.id, seller_id)

    result = await exchange_service.admin_force_cancel(session, chat_id, listing.id)

    assert result["status"] == "cancelled"
    assert result["refunded"] == 0
    assert await _get_user_balance(session, chat_id, seller_id) == seller_before


@pytest.mark.asyncio
async def test_admin_force_release_releases_claimed_listing(session):
    chat_id = -100920032
    seller_id, buyer_id = 920032, 920033
    await _ensure_user(session, seller_id)
    await _ensure_user(session, buyer_id)
    await _fund(session, chat_id, seller_id)
    buyer_before = await _fund(session, chat_id, buyer_id)

    listing = await exchange_service.create_listing(
        session, chat_id, seller_id, 100, "что-то", "test_admin_release_create"
    )
    await exchange_service.claim_listing(session, chat_id, listing.id, buyer_id)

    result = await exchange_service.admin_force_release(session, chat_id, listing.id)

    assert result["status"] == "fulfilled"
    assert result["released"] == 100
    assert result["claimed_by_user_id"] == buyer_id
    assert await _get_user_balance(session, chat_id, buyer_id) == buyer_before + 100


@pytest.mark.asyncio
async def test_admin_force_release_on_open_listing_raises(session):
    chat_id = -100920034
    seller_id = 920034
    await _ensure_user(session, seller_id)
    await _fund(session, chat_id, seller_id)

    listing = await exchange_service.create_listing(
        session, chat_id, seller_id, 100, "что-то", "test_admin_release_open_create"
    )

    with pytest.raises(exchange_service.ExchangeError):
        await exchange_service.admin_force_release(session, chat_id, listing.id)

    listing_row = await _get_listing(session, listing.id)
    assert listing_row.status == "open"


@pytest.mark.asyncio
async def test_admin_force_release_idempotent_on_terminal_status(session):
    chat_id = -100920035
    seller_id, buyer_id = 920035, 920036
    await _ensure_user(session, seller_id)
    await _ensure_user(session, buyer_id)
    await _fund(session, chat_id, seller_id)
    buyer_before = await _fund(session, chat_id, buyer_id)

    listing = await exchange_service.create_listing(
        session, chat_id, seller_id, 100, "что-то", "test_admin_release_twice_create"
    )
    await exchange_service.claim_listing(session, chat_id, listing.id, buyer_id)
    await exchange_service.admin_force_release(session, chat_id, listing.id)

    result = await exchange_service.admin_force_release(session, chat_id, listing.id)

    assert result["status"] == "fulfilled"
    assert result["released"] == 0
    assert await _get_user_balance(session, chat_id, buyer_id) == buyer_before + 100


# --- Read-хелперы --------------------------------------------------------


@pytest.mark.asyncio
async def test_get_open_listings_excludes_claimed_and_includes_seller_name(session):
    chat_id = -100920037
    seller_id, buyer_id = 920037, 920038
    await _ensure_user(session, seller_id, "Продавец Имя")
    await _ensure_user(session, buyer_id)
    await _fund(session, chat_id, seller_id)
    await _fund(session, chat_id, buyer_id)

    open_listing = await exchange_service.create_listing(
        session, chat_id, seller_id, 100, "открытый листинг", "test_open_listings_open_create"
    )
    claimed_listing = await exchange_service.create_listing(
        session, chat_id, seller_id, 50, "заклеймленный листинг", "test_open_listings_claimed_create"
    )
    await exchange_service.claim_listing(session, chat_id, claimed_listing.id, buyer_id)

    rows = await exchange_service.get_open_listings(session, chat_id)
    ids = {row["id"] for row in rows}

    assert open_listing.id in ids
    assert claimed_listing.id not in ids
    matching = next(row for row in rows if row["id"] == open_listing.id)
    assert matching["seller_name"] == "Продавец Имя"
    assert matching["yuvik_amount"] == 100


@pytest.mark.asyncio
async def test_get_my_listings_shows_both_seller_and_buyer_roles(session):
    chat_id = -100920039
    seller_id, buyer_id = 920039, 920040
    await _ensure_user(session, seller_id)
    await _ensure_user(session, buyer_id)
    await _fund(session, chat_id, seller_id)
    await _fund(session, chat_id, buyer_id)

    my_listing = await exchange_service.create_listing(
        session, chat_id, seller_id, 100, "мой листинг", "test_my_listings_seller_create"
    )
    others_listing = await exchange_service.create_listing(
        session, chat_id, seller_id, 50, "чужой листинг", "test_my_listings_buyer_create"
    )
    await exchange_service.claim_listing(session, chat_id, others_listing.id, buyer_id)

    seller_view = await exchange_service.get_my_listings(session, chat_id, seller_id)
    buyer_view = await exchange_service.get_my_listings(session, chat_id, buyer_id)

    seller_roles = {row["id"]: row["role"] for row in seller_view}
    assert seller_roles == {my_listing.id: "seller", others_listing.id: "seller"}

    buyer_roles = {row["id"]: row["role"] for row in buyer_view}
    assert buyer_roles == {others_listing.id: "buyer"}
