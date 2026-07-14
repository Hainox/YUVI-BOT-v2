"""Интеграционные тесты импорта/авторезолюции внешних рынков (BET-02) против
живого Postgres (фикстура `session` из tests/conftest.py). Реальный HTTP
полностью замокан через monkeypatch на `external_markets.fetch_external_market`
(по образцу tests/test_backfill_idempotency.py) — тесты детерминированы и не
зависят от живых Polymarket/Manifold API.

Task 1: import_market — создание, дедуп по (chat_id, type, external_id),
комиссия импорта (settings.market_import_fee) в банк, InsufficientFunds.

Task 2: auto_resolve_external — match winning_label→option.label (casefold),
resolve_market с той же 5%-комиссией (BET-03), skip на открытом источнике,
skip на label-mismatch (не гадаем).
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from bot.config import settings
from bot.services import economy_service
from bot.services import external_markets
from bot.services import markets_service
from common.models.bet import Bet
from common.models.market import Market
from common.models.market import MarketOption
from common.models.user import User
from common.models.user_balance import UserBalance


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
    from common.models.chat_bank import ChatBank

    result = await session.execute(select(ChatBank.balance).where(ChatBank.chat_id == chat_id))
    return result.scalar_one_or_none() or 0


async def _market_count(session, chat_id: int) -> int:
    result = await session.execute(
        select(Market.id).where(Market.chat_id == chat_id, Market.type != "internal")
    )
    return len(result.all())


def _fetched(
    question: str = "Будет ли X?",
    options: list[str] | None = None,
    closed: bool = False,
    winning_label: str | None = None,
    external_id: str = "cond-abc",
) -> dict:
    return {
        "question": question,
        "options": options or ["Yes", "No"],
        "closed": closed,
        "winning_label": winning_label,
        "external_id": external_id,
    }


# --- import_market: создание + комиссия --------------------------------------


@pytest.mark.asyncio
async def test_import_creates_market_and_charges_fee(session, monkeypatch):
    chat_id = -100900001
    creator_id = 920001
    await _ensure_user(session, creator_id)
    await _fund(session, chat_id, creator_id)

    monkeypatch.setattr(
        external_markets,
        "fetch_external_market",
        AsyncMock(return_value=_fetched(external_id="cond-import-1")),
    )

    market = await markets_service.import_market(
        session,
        chat_id,
        creator_id,
        "https://polymarket.com/market/will-x-happen",
        ref_id="test_import_creates:1",
    )

    assert market.type == "polymarket"
    assert market.status == "open"
    assert market.external_id == "cond-import-1"

    options = (
        await session.execute(select(MarketOption).where(MarketOption.market_id == market.id))
    ).scalars().all()
    assert {o.label for o in options} == {"Yes", "No"}

    assert await _get_user_balance(session, chat_id, creator_id) == (
        settings.economy_start_bonus - settings.market_import_fee
    )
    assert await _get_bank_balance(session, chat_id) == settings.market_import_fee


@pytest.mark.asyncio
async def test_import_manifold_url_sets_manifold_type(session, monkeypatch):
    chat_id = -100900002
    creator_id = 920002
    await _ensure_user(session, creator_id)
    await _fund(session, chat_id, creator_id)

    monkeypatch.setattr(
        external_markets,
        "fetch_external_market",
        AsyncMock(return_value=_fetched(external_id="mani-import-1")),
    )

    market = await markets_service.import_market(
        session,
        chat_id,
        creator_id,
        "https://manifold.markets/someuser/will-y-happen",
        ref_id="test_import_manifold:1",
    )

    assert market.type == "manifold"


# --- import_market: дедуп (T-03-24) ------------------------------------------


@pytest.mark.asyncio
async def test_import_dedup_second_call_rejected(session, monkeypatch):
    chat_id = -100900003
    creator_id = 920003
    await _ensure_user(session, creator_id)
    await _fund(session, chat_id, creator_id)

    monkeypatch.setattr(
        external_markets,
        "fetch_external_market",
        AsyncMock(return_value=_fetched(external_id="cond-dedup-1")),
    )

    await markets_service.import_market(
        session,
        chat_id,
        creator_id,
        "https://polymarket.com/market/dedup-market",
        ref_id="test_import_dedup:1",
    )
    balance_after_first = await _get_user_balance(session, chat_id, creator_id)
    bank_after_first = await _get_bank_balance(session, chat_id)

    with pytest.raises(markets_service.MarketAlreadyImported):
        await markets_service.import_market(
            session,
            chat_id,
            creator_id,
            "https://polymarket.com/market/dedup-market",
            ref_id="test_import_dedup:2",
        )

    assert await _get_user_balance(session, chat_id, creator_id) == balance_after_first
    assert await _get_bank_balance(session, chat_id) == bank_after_first
    assert await _market_count(session, chat_id) == 1


# --- import_market: недостаточно средств -------------------------------------


@pytest.mark.asyncio
async def test_import_insufficient_fee_raises(session, monkeypatch):
    chat_id = -100900004
    creator_id = 920004
    await _ensure_user(session, creator_id)
    # Заводим кошелёк, но обнуляем баланс ниже комиссии импорта.
    await _fund(session, chat_id, creator_id)
    await session.execute(
        UserBalance.__table__.update()
        .where(UserBalance.chat_id == chat_id, UserBalance.user_id == creator_id)
        .values(balance=settings.market_import_fee - 1)
    )
    await session.commit()

    monkeypatch.setattr(
        external_markets,
        "fetch_external_market",
        AsyncMock(return_value=_fetched(external_id="cond-insufficient-1")),
    )

    with pytest.raises(economy_service.InsufficientFunds):
        await markets_service.import_market(
            session,
            chat_id,
            creator_id,
            "https://polymarket.com/market/insufficient-market",
            ref_id="test_import_insufficient:1",
        )

    assert await _market_count(session, chat_id) == 0


# --- auto_resolve_external (BET-02 + BET-03) ---------------------------------


@pytest.mark.asyncio
async def test_auto_resolve_matches_label_and_pays_out(session, monkeypatch):
    chat_id = -100900005
    creator_id = 920005
    better_yes = 920006
    better_no = 920007
    await _ensure_user(session, creator_id)
    await _ensure_user(session, better_yes)
    await _ensure_user(session, better_no)
    await _fund(session, chat_id, creator_id)
    await _fund(session, chat_id, better_yes)
    await _fund(session, chat_id, better_no)

    monkeypatch.setattr(
        external_markets,
        "fetch_external_market",
        AsyncMock(return_value=_fetched(external_id="cond-autoresolve-1")),
    )
    market = await markets_service.import_market(
        session,
        chat_id,
        creator_id,
        "https://polymarket.com/market/autoresolve-market",
        ref_id="test_autoresolve_import:1",
    )

    await markets_service.place_bet(
        session, chat_id, market.id, better_yes, 1, 100, ref_id="test_autoresolve_bet:yes"
    )
    await markets_service.place_bet(
        session, chat_id, market.id, better_no, 2, 50, ref_id="test_autoresolve_bet:no"
    )

    bank_before_resolve = await _get_bank_balance(session, chat_id)

    monkeypatch.setattr(
        external_markets,
        "fetch_external_market",
        AsyncMock(
            return_value=_fetched(
                closed=True, winning_label="Yes", external_id="cond-autoresolve-1"
            )
        ),
    )

    await markets_service.auto_resolve_external(session)

    resolved = (await session.execute(select(Market).where(Market.id == market.id))).scalar_one()
    assert resolved.status == "resolved"

    bets = (await session.execute(select(Bet).where(Bet.market_id == market.id))).scalars().all()
    winning_bet = next(b for b in bets if b.user_id == better_yes)
    losing_bet = next(b for b in bets if b.user_id == better_no)
    assert winning_bet.payout > 0
    assert losing_bet.payout == 0

    assert await _get_bank_balance(session, chat_id) > bank_before_resolve


@pytest.mark.asyncio
async def test_auto_resolve_skips_still_open(session, monkeypatch):
    chat_id = -100900008
    creator_id = 920008
    await _ensure_user(session, creator_id)
    await _fund(session, chat_id, creator_id)

    monkeypatch.setattr(
        external_markets,
        "fetch_external_market",
        AsyncMock(return_value=_fetched(external_id="cond-stillopen-1")),
    )
    market = await markets_service.import_market(
        session,
        chat_id,
        creator_id,
        "https://polymarket.com/market/stillopen-market",
        ref_id="test_autoresolve_stillopen_import:1",
    )

    # fetch_external_market по-прежнему возвращает closed=False (тот же мок).
    await markets_service.auto_resolve_external(session)

    still_open = (
        await session.execute(select(Market).where(Market.id == market.id))
    ).scalar_one()
    assert still_open.status == "open"


@pytest.mark.asyncio
async def test_auto_resolve_label_mismatch_skips(session, monkeypatch):
    chat_id = -100900009
    creator_id = 920009
    await _ensure_user(session, creator_id)
    await _fund(session, chat_id, creator_id)

    monkeypatch.setattr(
        external_markets,
        "fetch_external_market",
        AsyncMock(return_value=_fetched(external_id="cond-mismatch-1")),
    )
    market = await markets_service.import_market(
        session,
        chat_id,
        creator_id,
        "https://polymarket.com/market/mismatch-market",
        ref_id="test_autoresolve_mismatch_import:1",
    )

    monkeypatch.setattr(
        external_markets,
        "fetch_external_market",
        AsyncMock(
            return_value=_fetched(
                closed=True,
                winning_label="Совсем другой вариант",
                external_id="cond-mismatch-1",
            )
        ),
    )

    await markets_service.auto_resolve_external(session)

    still_open = (
        await session.execute(select(Market).where(Market.id == market.id))
    ).scalar_one()
    assert still_open.status == "open"


@pytest.mark.asyncio
async def test_auto_resolve_transient_fetch_error_does_not_cancel_market(session, monkeypatch):
    chat_id = -100900010
    creator_id = 920010
    await _ensure_user(session, creator_id)
    await _fund(session, chat_id, creator_id)

    monkeypatch.setattr(
        external_markets,
        "fetch_external_market",
        AsyncMock(return_value=_fetched(external_id="cond-transient-1")),
    )
    market = await markets_service.import_market(
        session,
        chat_id,
        creator_id,
        "https://polymarket.com/market/transient-market",
        ref_id="test_autoresolve_transient_import:1",
    )

    monkeypatch.setattr(
        external_markets,
        "fetch_external_market",
        AsyncMock(side_effect=external_markets.MarketFetchError("сеть недоступна")),
    )

    await markets_service.auto_resolve_external(session)

    still_open = (
        await session.execute(select(Market).where(Market.id == market.id))
    ).scalar_one()
    assert still_open.status == "open"
