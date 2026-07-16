"""Тесты GET/POST /api/v1/markets* (Task 1/2, markets.py) против живого Postgres.

Тот же initData-хелпер, `_app_state`/`_fresh_engine_per_test`-фикстуры и
паттерн проверки IDOR через прямое чтение БД (не дельту баланса), что и
`tests/test_api_games.py` — денежная проверка идёт через `SessionLocal()`,
тот же engine, что использует сам роут (не транзакционная `session`-фикстура
conftest.py).

Рынки для тестов сеются напрямую через `bot.services.markets_service.
create_market` (открытый рынок с 2 вариантами) — та же функция, что
`/market_create` уже использует и тестирует в Фазе 3 (тест здесь НЕ
дублирует её собственную валидацию, только использует как сетап). Закрытый
рынок для 409-теста сеется прямой ORM-вставкой `Market`/`MarketOption` с
`closes_at` в прошлом (`create_market` не позволяет создать рынок с
длительностью короче 5 минут).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
import uuid
from datetime import datetime
from datetime import timedelta
from unittest.mock import AsyncMock
from urllib.parse import urlencode

import pytest
from httpx import ASGITransport
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy import update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from api import telegram_client
from api.main import app
from bot.config import settings
from bot.services import economy_service
from bot.services import markets_service
from common.db.session import engine
from common.db.session import SessionLocal
from common.models.bet import Bet
from common.models.market import Market
from common.models.market import MarketOption
from common.models.user import User

CHAT_ID = -900401


def _build_init_data(*, user_id: int, bot_token: str | None = None, tamper: bool = False) -> str:
    if bot_token is None:
        bot_token = settings.bot_token
    fields = {
        "auth_date": str(int(time.time())),
        "query_id": "AAABBBCCC",
        "user": json.dumps({"id": user_id, "first_name": "Тест"}),
    }
    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    computed_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()

    full = dict(fields)
    full["hash"] = ("0" * len(computed_hash)) if tamper else computed_hash
    return urlencode(full)


async def _ensure_user(user_id: int, first_name: str = "Тест") -> None:
    async with SessionLocal() as db_session:
        stmt = (
            pg_insert(User)
            .values(id=user_id, first_name=first_name)
            .on_conflict_do_nothing(index_elements=["id"])
        )
        await db_session.execute(stmt)
        await db_session.commit()


async def _fund(user_id: int, min_balance: int = 1000) -> None:
    """Заводит кошелёк (стартовый бонус `economy_start_bonus` при первом
    обращении) И топит баланс минимум до `min_balance`, если он уже ниже
    (Rule 1 fix, 04.2-10 — см. `deferred-items.md`).

    Тесты этого файла используют ФИКСИРОВАННЫЕ литералы `user_id` против
    ДОЛГОЖИВУЩЕГО docker-compose Postgres-контейнера (тот же engine, что
    использует сам роут, не транзакционная `session`-фикстура conftest.py
    с автоматическим rollback). `economy_service.get_balance` выдаёт
    `economy_start_bonus` ТОЛЬКО один раз на (chat_id, user_id) — повторные
    прогоны ПОЛНОГО набора тестов против одного и того же контейнера
    накапливают реальные списания на одних и тех же ID (комиссия создания
    рынка `market_create_fee`, ставки), со временем истощая стартовый
    бонус ниже сумм, которые тесты пытаются поставить — не баг кода
    04.2-07/04.2-10, чисто изоляция тестовых фикстур (подтверждено
    `git stash`-бисекцией в 04.2-08, см. `deferred-items.md`).

    `economy_service.credit` идемпотентен по `ref_id` — используем СВЕЖИЙ
    `ref_id` (`uuid4()`) на каждый вызов, поэтому пополнение реально
    применяется КАЖДЫЙ прогон, а не гасится идемпотентным no-op."""
    async with SessionLocal() as db_session:
        balance = await economy_service.get_balance(db_session, CHAT_ID, user_id)
        if balance < min_balance:
            await economy_service.credit(
                db_session,
                CHAT_ID,
                user_id,
                min_balance - balance,
                kind="test_topup",
                ref_id=f"test_topup:{uuid.uuid4()}",
            )
            await db_session.commit()


async def _seed_open_market(creator_id: int) -> int:
    await _ensure_user(creator_id)
    await _fund(creator_id)
    async with SessionLocal() as db_session:
        market = await markets_service.create_market(
            db_session,
            CHAT_ID,
            creator_id,
            "Тестовый рынок: пойдёт дождь?",
            ["Да", "Нет"],
            "7d",
            ref_id=str(uuid.uuid4()),
        )
        return market.id


async def _seed_closed_market(creator_id: int) -> int:
    await _ensure_user(creator_id)
    async with SessionLocal() as db_session:
        market = Market(
            chat_id=CHAT_ID,
            type="internal",
            question="Закрытый рынок",
            creator_id=creator_id,
            status="open",
            closes_at=datetime.utcnow() - timedelta(minutes=1),
        )
        db_session.add(market)
        await db_session.flush()
        db_session.add(MarketOption(market_id=market.id, label="Да", pool=0, position=1))
        db_session.add(MarketOption(market_id=market.id, label="Нет", pool=0, position=2))
        await db_session.commit()
        return market.id


async def _get_balance(user_id: int) -> int:
    async with SessionLocal() as db_session:
        return await economy_service.get_balance(db_session, CHAT_ID, user_id)


@pytest.fixture(autouse=True)
def _reset_membership_cache():
    telegram_client.reset_cache()
    yield
    telegram_client.reset_cache()


@pytest.fixture(autouse=True)
async def _fresh_engine_per_test():
    await engine.dispose()
    yield
    await engine.dispose()


@pytest.fixture(autouse=True)
def _app_state():
    app.state.http_client = AsyncMock()
    app.state.redis = None
    yield


# --- GET /api/v1/markets ------------------------------------------------


@pytest.mark.asyncio
async def test_list_markets_returns_open_market(monkeypatch):
    monkeypatch.setattr(telegram_client, "get_chat_member_status", AsyncMock(return_value="member"))
    creator_id = 400101
    market_id = await _seed_open_market(creator_id)
    init_data = _build_init_data(user_id=creator_id)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            "/api/v1/markets",
            params={"chat_id": CHAT_ID},
            headers={"X-Telegram-Init-Data": init_data},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert any(m["id"] == market_id for m in body)


@pytest.mark.asyncio
async def test_list_markets_missing_init_data_returns_401():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/v1/markets", params={"chat_id": CHAT_ID})

    assert resp.status_code == 401


# --- GET /api/v1/markets/{id} --------------------------------------------


@pytest.mark.asyncio
async def test_market_detail_returns_options_and_pools(monkeypatch):
    monkeypatch.setattr(telegram_client, "get_chat_member_status", AsyncMock(return_value="member"))
    creator_id = 400102
    market_id = await _seed_open_market(creator_id)
    init_data = _build_init_data(user_id=creator_id)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            f"/api/v1/markets/{market_id}",
            params={"chat_id": CHAT_ID},
            headers={"X-Telegram-Init-Data": init_data},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == market_id
    assert len(body["options"]) == 2
    assert {opt["label"] for opt in body["options"]} == {"Да", "Нет"}


@pytest.mark.asyncio
async def test_market_detail_nonexistent_returns_404(monkeypatch):
    monkeypatch.setattr(telegram_client, "get_chat_member_status", AsyncMock(return_value="member"))
    user_id = 400103
    await _ensure_user(user_id)
    init_data = _build_init_data(user_id=user_id)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            "/api/v1/markets/9999999",
            params={"chat_id": CHAT_ID},
            headers={"X-Telegram-Init-Data": init_data},
        )

    assert resp.status_code == 404


# --- POST /api/v1/markets/{id}/bets --------------------------------------


@pytest.mark.asyncio
async def test_place_bet_valid_returns_200_and_records_bet(monkeypatch):
    monkeypatch.setattr(telegram_client, "get_chat_member_status", AsyncMock(return_value="member"))
    creator_id = 400104
    bettor_id = 400105
    market_id = await _seed_open_market(creator_id)
    await _ensure_user(bettor_id)
    await _fund(bettor_id)
    init_data = _build_init_data(user_id=bettor_id)
    ref_id = str(uuid.uuid4())

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            f"/api/v1/markets/{market_id}/bets",
            params={"chat_id": CHAT_ID},
            headers={"X-Telegram-Init-Data": init_data},
            json={"option_position": 1, "amount": 20, "ref_id": ref_id},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["replayed"] is False
    assert body["amount"] == 20
    assert "user_balance_after" in body

    async with SessionLocal() as verify_session:
        bet_row = (
            await verify_session.execute(select(Bet).where(Bet.market_id == market_id))
        ).scalar_one()
    assert bet_row.user_id == bettor_id
    assert bet_row.amount == 20


@pytest.mark.asyncio
async def test_place_bet_below_minimum_returns_400(monkeypatch):
    monkeypatch.setattr(telegram_client, "get_chat_member_status", AsyncMock(return_value="member"))
    creator_id = 400106
    bettor_id = 400107
    market_id = await _seed_open_market(creator_id)
    await _ensure_user(bettor_id)
    await _fund(bettor_id)
    init_data = _build_init_data(user_id=bettor_id)
    assert 1 < settings.market_min_bet  # гарантирует, что amount=1 реально ниже минимума

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            f"/api/v1/markets/{market_id}/bets",
            params={"chat_id": CHAT_ID},
            headers={"X-Telegram-Init-Data": init_data},
            json={"option_position": 1, "amount": 1, "ref_id": str(uuid.uuid4())},
        )

    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_place_bet_invalid_option_returns_400(monkeypatch):
    monkeypatch.setattr(telegram_client, "get_chat_member_status", AsyncMock(return_value="member"))
    creator_id = 400108
    bettor_id = 400109
    market_id = await _seed_open_market(creator_id)
    await _ensure_user(bettor_id)
    await _fund(bettor_id)
    init_data = _build_init_data(user_id=bettor_id)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            f"/api/v1/markets/{market_id}/bets",
            params={"chat_id": CHAT_ID},
            headers={"X-Telegram-Init-Data": init_data},
            json={"option_position": 99, "amount": 20, "ref_id": str(uuid.uuid4())},
        )

    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_place_bet_nonexistent_market_returns_404(monkeypatch):
    monkeypatch.setattr(telegram_client, "get_chat_member_status", AsyncMock(return_value="member"))
    user_id = 400110
    await _ensure_user(user_id)
    await _fund(user_id)
    init_data = _build_init_data(user_id=user_id)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/markets/9999999/bets",
            params={"chat_id": CHAT_ID},
            headers={"X-Telegram-Init-Data": init_data},
            json={"option_position": 1, "amount": 20, "ref_id": str(uuid.uuid4())},
        )

    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_place_bet_closed_market_returns_409(monkeypatch):
    monkeypatch.setattr(telegram_client, "get_chat_member_status", AsyncMock(return_value="member"))
    creator_id = 400111
    bettor_id = 400112
    market_id = await _seed_closed_market(creator_id)
    await _ensure_user(bettor_id)
    await _fund(bettor_id)
    init_data = _build_init_data(user_id=bettor_id)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            f"/api/v1/markets/{market_id}/bets",
            params={"chat_id": CHAT_ID},
            headers={"X-Telegram-Init-Data": init_data},
            json={"option_position": 1, "amount": 20, "ref_id": str(uuid.uuid4())},
        )

    assert resp.status_code == 409

    # Cleanup: this market intentionally sits at status="open" with
    # closes_at in the past (needed to trigger MarketClosed via place_bet's
    # OR-condition) — left as-is it would pollute markets_service.
    # auto_close_expired's GLOBAL (not chat-scoped) count on the next full
    # test-suite run, the same class of live-Postgres cross-test pollution
    # already documented/fixed for test_api_farm.py/test_api_gacha.py
    # (04.2-04/04.2-05 Rule 1 fixes).
    async with SessionLocal() as cleanup_session:
        await cleanup_session.execute(
            update(Market).where(Market.id == market_id).values(status="cancelled")
        )
        await cleanup_session.commit()


@pytest.mark.asyncio
async def test_place_bet_repeated_ref_id_is_idempotent_no_op(monkeypatch):
    """Повтор того же ref_id — `place_bet` возвращает None (не ошибка);
    роут обязан ответить `replayed: true`, не списывать деньги повторно."""
    monkeypatch.setattr(telegram_client, "get_chat_member_status", AsyncMock(return_value="member"))
    creator_id = 400113
    bettor_id = 400114
    market_id = await _seed_open_market(creator_id)
    await _ensure_user(bettor_id)
    await _fund(bettor_id)
    init_data = _build_init_data(user_id=bettor_id)
    ref_id = str(uuid.uuid4())

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        first = await client.post(
            f"/api/v1/markets/{market_id}/bets",
            params={"chat_id": CHAT_ID},
            headers={"X-Telegram-Init-Data": init_data},
            json={"option_position": 1, "amount": 20, "ref_id": ref_id},
        )
        assert first.status_code == 200
        balance_after_first = await _get_balance(bettor_id)

        second = await client.post(
            f"/api/v1/markets/{market_id}/bets",
            params={"chat_id": CHAT_ID},
            headers={"X-Telegram-Init-Data": init_data},
            json={"option_position": 1, "amount": 20, "ref_id": ref_id},
        )

    assert second.status_code == 200
    assert second.json()["replayed"] is True
    balance_after_second = await _get_balance(bettor_id)
    assert balance_after_second == balance_after_first  # деньги не списаны повторно

    async with SessionLocal() as verify_session:
        bet_count = (
            await verify_session.execute(select(Bet).where(Bet.market_id == market_id))
        ).scalars().all()
    assert len(bet_count) == 1  # одна ставка, не две


@pytest.mark.asyncio
async def test_place_bet_ignores_foreign_user_id_in_body_idor(monkeypatch):
    """T-04.2-02 (IDOR): user_id берётся ТОЛЬКО из AuthContext — поддельный
    user_id "жертвы" в теле запроса не должен сдвинуть её баланс/ставку."""
    monkeypatch.setattr(telegram_client, "get_chat_member_status", AsyncMock(return_value="member"))
    creator_id = 400115
    attacker_id = 400116
    victim_id = 400117
    market_id = await _seed_open_market(creator_id)
    await _ensure_user(attacker_id)
    await _fund(attacker_id)
    await _ensure_user(victim_id)
    await _fund(victim_id)
    init_data = _build_init_data(user_id=attacker_id)
    ref_id = str(uuid.uuid4())

    victim_before = await _get_balance(victim_id)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            f"/api/v1/markets/{market_id}/bets",
            params={"chat_id": CHAT_ID},
            headers={"X-Telegram-Init-Data": init_data},
            json={
                "option_position": 1,
                "amount": 20,
                "ref_id": ref_id,
                "user_id": victim_id,  # атакующий пытается подставить чужой user_id
            },
        )

    assert resp.status_code == 200

    async with SessionLocal() as verify_session:
        bet_row = (
            await verify_session.execute(select(Bet).where(Bet.market_id == market_id))
        ).scalar_one()
    assert bet_row.user_id == attacker_id

    victim_after = await _get_balance(victim_id)
    assert victim_after == victim_before  # жертва не затронута вовсе


# --- GET /api/v1/markets/portfolio ---------------------------------------


@pytest.mark.asyncio
async def test_portfolio_returns_user_open_position(monkeypatch):
    monkeypatch.setattr(telegram_client, "get_chat_member_status", AsyncMock(return_value="member"))
    creator_id = 400118
    bettor_id = 400119
    market_id = await _seed_open_market(creator_id)
    await _ensure_user(bettor_id)
    await _fund(bettor_id)
    init_data = _build_init_data(user_id=bettor_id)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await client.post(
            f"/api/v1/markets/{market_id}/bets",
            params={"chat_id": CHAT_ID},
            headers={"X-Telegram-Init-Data": init_data},
            json={"option_position": 1, "amount": 20, "ref_id": str(uuid.uuid4())},
        )

        resp = await client.get(
            "/api/v1/markets/portfolio",
            params={"chat_id": CHAT_ID},
            headers={"X-Telegram-Init-Data": init_data},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert any(p["market_id"] == market_id and p["amount"] == 20 for p in body)


@pytest.mark.asyncio
async def test_portfolio_only_returns_authenticated_users_bets(monkeypatch):
    """Портфолио читает ТОЛЬКО ставки пользователя из AuthContext (IDOR)."""
    monkeypatch.setattr(telegram_client, "get_chat_member_status", AsyncMock(return_value="member"))
    creator_id = 400120
    bettor_a = 400121
    bettor_b = 400122
    market_id = await _seed_open_market(creator_id)
    await _ensure_user(bettor_a)
    await _fund(bettor_a)
    await _ensure_user(bettor_b)
    await _fund(bettor_b)
    init_data_a = _build_init_data(user_id=bettor_a)
    init_data_b = _build_init_data(user_id=bettor_b)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await client.post(
            f"/api/v1/markets/{market_id}/bets",
            params={"chat_id": CHAT_ID},
            headers={"X-Telegram-Init-Data": init_data_a},
            json={"option_position": 1, "amount": 20, "ref_id": str(uuid.uuid4())},
        )

        resp_b = await client.get(
            "/api/v1/markets/portfolio",
            params={"chat_id": CHAT_ID},
            headers={"X-Telegram-Init-Data": init_data_b},
        )

    assert resp_b.status_code == 200
    assert resp_b.json() == []  # bettor_b не ставил — портфолио пустое
