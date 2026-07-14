"""Интеграционные тесты markets_service против живого Postgres (фикстура
`session` из tests/conftest.py — транзакция-на-тест). Доказывают BET-01:
лимиты создания рынка (D-05), комиссия создания 100 ювиков в банк,
parimutuel-рост пула ставки, идемпотентность place_bet по ref_id, а также
формат-хелперы bot/handlers/markets.py (html.escape вопроса/вариантов —
Pitfall 6).

markets_service сам делает session.commit() там, где это описано в его
контракте — совместимо с фикстурой session благодаря join-savepoint режиму
SQLAlchemy 2.0 (тот же паттерн уже проверен в test_economy_service.py).
"""

from __future__ import annotations

import inspect
from datetime import timedelta

import pytest
from sqlalchemy import select

from bot.config import settings
from bot.handlers.markets import format_market_detail
from bot.services import economy_service
from bot.services import markets_service
from common.models.bet import Bet
from common.models.chat_bank import ChatBank
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
    result = await session.execute(select(ChatBank.balance).where(ChatBank.chat_id == chat_id))
    return result.scalar_one_or_none() or 0


# --- parse_duration (D-05) --------------------------------------------------


def test_parse_duration_valid():
    assert markets_service.parse_duration("7d") == timedelta(days=7)
    assert markets_service.parse_duration("12h") == timedelta(hours=12)
    assert markets_service.parse_duration("90m") == timedelta(minutes=90)
    assert markets_service.parse_duration("7d|12h|90m") == timedelta(
        days=7, hours=12, minutes=90
    )
    # пустые токены игнорируются
    assert markets_service.parse_duration("7d||12h") == timedelta(days=7, hours=12)
    assert markets_service.parse_duration(" 7d | 12h ") == timedelta(days=7, hours=12)


@pytest.mark.parametrize("raw", ["7x", "abc", "", "   ", "-5d", "5", "5dd"])
def test_parse_duration_invalid_raises(raw):
    with pytest.raises(markets_service.DurationError):
        markets_service.parse_duration(raw)


# --- create_market лимиты и комиссия (D-05) ---------------------------------


@pytest.mark.asyncio
async def test_create_market_enforces_limits(session):
    chat_id = -100800001
    creator_id = 810001
    await _ensure_user(session, creator_id)
    await _fund(session, chat_id, creator_id)

    with pytest.raises(markets_service.InvalidMarketArg):
        await markets_service.create_market(
            session, chat_id, creator_id, "Коро", ["A", "B"], "7d", "test_limits_question_short"
        )

    with pytest.raises(markets_service.InvalidMarketArg):
        await markets_service.create_market(
            session,
            chat_id,
            creator_id,
            "Достаточно длинный вопрос для проверки?",
            ["A"],
            "7d",
            "test_limits_options_few",
        )

    with pytest.raises(markets_service.InvalidMarketArg):
        await markets_service.create_market(
            session,
            chat_id,
            creator_id,
            "Достаточно длинный вопрос для проверки?",
            ["A", "B", "C", "D", "E", "F", "G"],
            "7d",
            "test_limits_options_many",
        )

    with pytest.raises(markets_service.InvalidMarketArg):
        await markets_service.create_market(
            session,
            chat_id,
            creator_id,
            "Достаточно длинный вопрос для проверки?",
            ["A", "B"],
            "1m",
            "test_limits_duration_short",
        )

    with pytest.raises(markets_service.InvalidMarketArg):
        await markets_service.create_market(
            session,
            chat_id,
            creator_id,
            "Достаточно длинный вопрос для проверки?",
            ["A", "B"],
            "400d",
            "test_limits_duration_long",
        )

    # ни один из невалидных вызовов не должен был создать рынок или списать деньги
    markets = (
        await session.execute(select(Market).where(Market.chat_id == chat_id))
    ).scalars().all()
    assert markets == []
    assert await _get_user_balance(session, chat_id, creator_id) == settings.economy_start_bonus


@pytest.mark.asyncio
async def test_create_market_charges_100_fee_to_bank(session):
    chat_id = -100800002
    creator_id = 810010
    await _ensure_user(session, creator_id)
    await _fund(session, chat_id, creator_id)

    market = await markets_service.create_market(
        session,
        chat_id,
        creator_id,
        "Кто выиграет турнир по CS?",
        ["Команда А", "Команда Б", "Ничья"],
        "7d",
        "test_create_market_fee",
    )

    assert market.id is not None
    assert market.type == "internal"
    assert market.status == "open"

    assert (
        await _get_user_balance(session, chat_id, creator_id)
        == settings.economy_start_bonus - settings.market_creation_fee
    )
    assert await _get_bank_balance(session, chat_id) == settings.market_creation_fee

    options = (
        await session.execute(
            select(MarketOption)
            .where(MarketOption.market_id == market.id)
            .order_by(MarketOption.position)
        )
    ).scalars().all()
    assert [o.position for o in options] == [1, 2, 3]
    assert [o.pool for o in options] == [0, 0, 0]
    assert [o.label for o in options] == ["Команда А", "Команда Б", "Ничья"]


@pytest.mark.asyncio
async def test_create_market_insufficient_fee_raises(session):
    chat_id = -100800003
    creator_id = 810020
    await _ensure_user(session, creator_id)
    await _fund(session, chat_id, creator_id)

    # спускаем баланс ниже комиссии создания рынка
    leave = settings.market_creation_fee - 1
    spend = settings.economy_start_bonus - leave
    assert await economy_service.debit(
        session, chat_id, creator_id, spend, "test_setup", "test_insufficient_setup"
    )
    await session.commit()

    with pytest.raises(economy_service.InsufficientFunds):
        await markets_service.create_market(
            session,
            chat_id,
            creator_id,
            "Кто выиграет турнир по CS?",
            ["A", "B"],
            "7d",
            "test_insufficient_fee",
        )

    markets = (
        await session.execute(select(Market).where(Market.chat_id == chat_id))
    ).scalars().all()
    assert markets == []
    assert await _get_user_balance(session, chat_id, creator_id) == leave


# --- place_bet (parimutuel, идемпотентно) -----------------------------------


@pytest.mark.asyncio
async def test_place_bet_debits_and_grows_pool(session):
    chat_id = -100800004
    creator_id, bettor_id = 810030, 810031
    await _ensure_user(session, creator_id)
    await _ensure_user(session, bettor_id)
    await _fund(session, chat_id, creator_id)
    await _fund(session, chat_id, bettor_id)

    market = await markets_service.create_market(
        session,
        chat_id,
        creator_id,
        "Пойдёт ли дождь завтра?",
        ["Да", "Нет"],
        "7d",
        "test_bet_setup_market",
    )

    bet = await markets_service.place_bet(session, chat_id, market.id, bettor_id, 1, 50, "test_bet_place")

    assert bet is not None
    assert bet.amount == 50

    assert await _get_user_balance(session, chat_id, bettor_id) == settings.economy_start_bonus - 50

    option = (
        await session.execute(
            select(MarketOption).where(
                MarketOption.market_id == market.id, MarketOption.position == 1
            )
        )
    ).scalar_one()
    assert option.pool == 50

    bets = (await session.execute(select(Bet).where(Bet.market_id == market.id))).scalars().all()
    assert len(bets) == 1


@pytest.mark.asyncio
async def test_place_bet_below_min_rejected(session):
    chat_id = -100800005
    creator_id, bettor_id = 810040, 810041
    await _ensure_user(session, creator_id)
    await _ensure_user(session, bettor_id)
    await _fund(session, chat_id, creator_id)
    balance_before = await _fund(session, chat_id, bettor_id)

    market = await markets_service.create_market(
        session,
        chat_id,
        creator_id,
        "Пойдёт ли дождь завтра?",
        ["Да", "Нет"],
        "7d",
        "test_below_min_setup",
    )

    with pytest.raises(markets_service.InvalidMarketArg):
        await markets_service.place_bet(
            session, chat_id, market.id, bettor_id, 1, settings.market_min_bet - 1, "test_below_min_bet"
        )

    assert await _get_user_balance(session, chat_id, bettor_id) == balance_before


@pytest.mark.asyncio
async def test_place_bet_on_closed_market_rejected(session):
    chat_id = -100800006
    creator_id, bettor_id = 810050, 810051
    await _ensure_user(session, creator_id)
    await _ensure_user(session, bettor_id)
    await _fund(session, chat_id, creator_id)
    await _fund(session, chat_id, bettor_id)

    market = await markets_service.create_market(
        session,
        chat_id,
        creator_id,
        "Пойдёт ли дождь завтра?",
        ["Да", "Нет"],
        "7d",
        "test_closed_setup",
    )
    market.status = "closed"
    await session.commit()

    with pytest.raises(markets_service.MarketClosed):
        await markets_service.place_bet(session, chat_id, market.id, bettor_id, 1, 50, "test_closed_bet")


@pytest.mark.asyncio
async def test_place_bet_idempotent_on_retry(session):
    chat_id = -100800007
    creator_id, bettor_id = 810060, 810061
    await _ensure_user(session, creator_id)
    await _ensure_user(session, bettor_id)
    await _fund(session, chat_id, creator_id)
    await _fund(session, chat_id, bettor_id)

    market = await markets_service.create_market(
        session,
        chat_id,
        creator_id,
        "Пойдёт ли дождь завтра?",
        ["Да", "Нет"],
        "7d",
        "test_idempotent_setup",
    )

    ref_id = "test_idempotent_bet"
    first = await markets_service.place_bet(session, chat_id, market.id, bettor_id, 1, 50, ref_id)
    assert first is not None

    second = await markets_service.place_bet(session, chat_id, market.id, bettor_id, 1, 50, ref_id)
    assert second is None

    assert await _get_user_balance(session, chat_id, bettor_id) == settings.economy_start_bonus - 50

    option = (
        await session.execute(
            select(MarketOption).where(
                MarketOption.market_id == market.id, MarketOption.position == 1
            )
        )
    ).scalar_one()
    assert option.pool == 50

    bets = (
        await session.execute(
            select(Bet).where(Bet.market_id == market.id, Bet.user_id == bettor_id)
        )
    ).scalars().all()
    assert len(bets) == 1


# --- read-хелперы ------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_open_markets_and_detail_and_portfolio(session):
    chat_id = -100800008
    creator_id, bettor_id = 810070, 810071
    await _ensure_user(session, creator_id)
    await _ensure_user(session, bettor_id)
    await _fund(session, chat_id, creator_id)
    await _fund(session, chat_id, bettor_id)

    market = await markets_service.create_market(
        session,
        chat_id,
        creator_id,
        "Кто победит в финале?",
        ["Команда А", "Команда Б"],
        "7d",
        "test_read_helpers_setup",
    )
    await markets_service.place_bet(session, chat_id, market.id, bettor_id, 1, 100, "test_read_helpers_bet")

    open_markets = await markets_service.get_open_markets(session, chat_id)
    assert len(open_markets) == 1
    assert open_markets[0]["id"] == market.id
    assert open_markets[0]["options_count"] == 2

    detail = await markets_service.get_market_detail(session, chat_id, market.id)
    assert detail["total_pool"] == 100
    assert detail["options"][0]["pool"] == 100
    assert detail["options"][0]["share_pct"] == 100.0
    assert detail["options"][1]["pool"] == 0

    with pytest.raises(markets_service.MarketNotFound):
        await markets_service.get_market_detail(session, chat_id, market.id + 999_999)

    portfolio = await markets_service.get_user_portfolio(session, chat_id, bettor_id)
    assert len(portfolio) == 1
    assert portfolio[0]["market_id"] == market.id
    assert portfolio[0]["option_label"] == "Команда А"
    assert portfolio[0]["amount"] == 100


# --- Money-integrity: markets_service не пишет балансы напрямую ------------


def test_markets_service_never_writes_balances_directly():
    """Статическая проверка исходника: markets_service — не economy_service,
    и НЕ имеет права писать user_balance/chat_bank напрямую (только через
    economy_service.debit/credit/credit_bank)."""
    source = inspect.getsource(markets_service)
    assert "update(UserBalance" not in source
    assert "update(ChatBank" not in source
    assert "pg_insert(UserBalance" not in source
    assert "pg_insert(ChatBank" not in source
    assert "insert(UserBalance" not in source
    assert "insert(ChatBank" not in source


# --- format_market_detail escaping (Pitfall 6, T-03-13) --------------------


def test_format_market_detail_escapes_question():
    detail = {
        "id": 1,
        "question": "<b>Опасный</b> вопрос & <script>alert(1)</script>",
        "status": "open",
        "total_pool": 0,
        "closes_at": None,
        "options": [
            {"position": 1, "label": "<i>вариант</i>", "pool": 0, "share_pct": 0.0},
            {"position": 2, "label": "обычный", "pool": 0, "share_pct": 0.0},
        ],
    }
    # closes_at=None не встречается в реальных данных (Market.closes_at NOT
    # NULL), но format_market_detail форматирует его после escape-проверки —
    # проверяем экранирование через detail с валидной датой отдельно ниже.
    from datetime import datetime as _dt

    detail["closes_at"] = _dt(2026, 1, 1, 12, 0)

    text = format_market_detail(detail)

    assert "<b>Опасный</b>" not in text
    assert "&lt;b&gt;Опасный&lt;/b&gt;" in text
    assert "<script>" not in text
    assert "&lt;i&gt;вариант&lt;/i&gt;" in text
