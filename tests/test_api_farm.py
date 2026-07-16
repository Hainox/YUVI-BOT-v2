"""Тесты POST/GET /api/v1/farm* (Task 1/2, api/routes/farm.py) — против живого
Postgres. Тот же initData-хелпер/фикстуры, что и `test_api_games.py`
(независимая от `api/deps.py` сборка initData, monkeypatch членства,
`engine.dispose()` между тестами из-за pytest-asyncio function-scoped loop).

Роуты — тонкая обёртка над `clicker_service` (уже полностью протестирован
против anti-cheat/оффлайн-накопления/апгрейдов в `test_clicker_service.py`
Фазы 04.1) — здесь проверяется только HTTP-контракт: auth (401 без initData),
маппинг `ClickerError` -> 400, и что anti-cheat клэмп `tap()` (MAX_CPS) реально
доходит до клиента через роут (заявленный клиентом `count` не даёт больше CP,
чем позволяет сервер), а не переопределяется/ослабляется роутом (T-04.2-07).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
import uuid
from unittest.mock import AsyncMock
from urllib.parse import urlencode

import pytest
from httpx import ASGITransport
from httpx import AsyncClient
from sqlalchemy import update

from api import telegram_client
from api.main import app
from bot.config import settings
from bot.services import clicker_service
from common.db.session import engine
from common.db.session import SessionLocal
from common.models.clicker_farm import ClickerFarm
from common.models.user import User
from sqlalchemy.dialects.postgresql import insert as pg_insert

CHAT_ID = -900401
UPGRADE_CHAT_ID = -900402
CONVERT_CHAT_ID = -900403


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


async def _seed_farm_cp(chat_id: int, user_id: int, cp: int) -> None:
    """Гарантирует существование строки фермы и выставляет `cp` напрямую (в
    обход тапов/апгрейдов) — детерминированная подготовка баланса CP для
    тестов апгрейда/конвертации без прохода через анти-чит тапа.

    `tap_level`/`auto_level` ЯВНО сбрасываются к начальным значениям (не
    только `cp`) — эти HTTP-тесты коммитят в реальный Postgres напрямую (в
    отличие от `session`-фикстуры conftest.py, которая откатывает
    транзакцию), поэтому повторный прогон тестового набора против того же
    контейнера без пересоздания БД иначе застал бы уже прокачанный с
    прошлого прогона уровень и сделал бы ассерты на конкретный `tap_level`
    недетерминированными."""
    async with SessionLocal() as db_session:
        await clicker_service.get_farm_state(db_session, chat_id, user_id)
        await db_session.execute(
            update(ClickerFarm)
            .where(ClickerFarm.chat_id == chat_id, ClickerFarm.user_id == user_id)
            .values(cp=cp, tap_level=1, auto_level=0)
        )
        await db_session.commit()


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


# --- GET /farm -----------------------------------------------------------


@pytest.mark.asyncio
async def test_get_farm_returns_state_200(monkeypatch):
    monkeypatch.setattr(telegram_client, "get_chat_member_status", AsyncMock(return_value="member"))
    user_id = 400101
    await _ensure_user(user_id)
    init_data = _build_init_data(user_id=user_id)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            "/api/v1/farm",
            params={"chat_id": CHAT_ID},
            headers={"X-Telegram-Init-Data": init_data},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["cp"] == 0
    assert body["tap_level"] == 1
    assert body["auto_level"] == 0
    assert "cp_per_sec" in body


@pytest.mark.asyncio
async def test_get_farm_missing_init_data_returns_401():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/v1/farm", params={"chat_id": CHAT_ID})

    assert resp.status_code == 401


# --- POST /farm/tap (anti-cheat clamp reaches the client, T-04.2-07) -----


@pytest.mark.asyncio
async def test_tap_forwards_count_and_elapsed_returns_state(monkeypatch):
    monkeypatch.setattr(telegram_client, "get_chat_member_status", AsyncMock(return_value="member"))
    user_id = 400102
    await _ensure_user(user_id)
    init_data = _build_init_data(user_id=user_id)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/farm/tap",
            params={"chat_id": CHAT_ID},
            headers={"X-Telegram-Init-Data": init_data},
            json={"count": 3, "elapsed_ms": 1000},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["accepted"] <= 3
    assert body["cp"] >= 0


@pytest.mark.asyncio
async def test_tap_wildly_large_count_does_not_credit_more_than_server_allows(monkeypatch):
    """T-04.2-07: клиент заявляет count=100000 за 1с — роут ДОЛЖЕН прокинуть
    его as-is в clicker_service.tap, где anti-cheat реально клэмпит принятые
    тапы потолком MAX_CPS (не ослабляется на уровне роута)."""
    monkeypatch.setattr(telegram_client, "get_chat_member_status", AsyncMock(return_value="member"))
    user_id = 400103
    await _ensure_user(user_id)
    init_data = _build_init_data(user_id=user_id)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/farm/tap",
            params={"chat_id": CHAT_ID},
            headers={"X-Telegram-Init-Data": init_data},
            json={"count": 100_000, "elapsed_ms": 1000},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["accepted"] <= clicker_service.MAX_CPS
    assert body["accepted"] < 100_000


@pytest.mark.asyncio
async def test_tap_missing_init_data_returns_401():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/farm/tap",
            params={"chat_id": CHAT_ID},
            json={"count": 3, "elapsed_ms": 1000},
        )

    assert resp.status_code == 401


# --- POST /farm/upgrade/tap /farm/upgrade/auto (ClickerError -> 400) -----


@pytest.mark.asyncio
async def test_upgrade_tap_insufficient_cp_returns_400(monkeypatch):
    monkeypatch.setattr(telegram_client, "get_chat_member_status", AsyncMock(return_value="member"))
    user_id = 400104
    await _ensure_user(user_id)
    init_data = _build_init_data(user_id=user_id)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/farm/upgrade/tap",
            params={"chat_id": UPGRADE_CHAT_ID},
            headers={"X-Telegram-Init-Data": init_data},
        )

    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_upgrade_auto_insufficient_cp_returns_400(monkeypatch):
    monkeypatch.setattr(telegram_client, "get_chat_member_status", AsyncMock(return_value="member"))
    user_id = 400105
    await _ensure_user(user_id)
    init_data = _build_init_data(user_id=user_id)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/farm/upgrade/auto",
            params={"chat_id": UPGRADE_CHAT_ID},
            headers={"X-Telegram-Init-Data": init_data},
        )

    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_upgrade_tap_succeeds_with_enough_cp(monkeypatch):
    monkeypatch.setattr(telegram_client, "get_chat_member_status", AsyncMock(return_value="member"))
    user_id = 400106
    await _ensure_user(user_id)
    await _seed_farm_cp(UPGRADE_CHAT_ID, user_id, 100_000)
    init_data = _build_init_data(user_id=user_id)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/farm/upgrade/tap",
            params={"chat_id": UPGRADE_CHAT_ID},
            headers={"X-Telegram-Init-Data": init_data},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["tap_level"] == 2


# --- POST /farm/convert (100 CP = 1 ювик direction, FARM-01) -------------


@pytest.mark.asyncio
async def test_convert_cp_to_hryvnia(monkeypatch):
    monkeypatch.setattr(telegram_client, "get_chat_member_status", AsyncMock(return_value="member"))
    user_id = 400107
    await _ensure_user(user_id)
    await _seed_farm_cp(CONVERT_CHAT_ID, user_id, 10_000)
    init_data = _build_init_data(user_id=user_id)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/farm/convert",
            params={"chat_id": CONVERT_CHAT_ID},
            headers={"X-Telegram-Init-Data": init_data},
            json={"cp_in": 1000, "ref_id": str(uuid.uuid4())},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["cp_in"] == 1000
    # Anchor is 100 CP = 1 hryvnia; on freshly-seeded pool reserves the AMM
    # price impact for 1000 CP is negligible — expect ~10 hryvnia.
    assert 9 <= body["hryvnia_out"] <= 10


@pytest.mark.asyncio
async def test_convert_missing_init_data_returns_401():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/farm/convert",
            params={"chat_id": CONVERT_CHAT_ID},
            json={"cp_in": 1000, "ref_id": str(uuid.uuid4())},
        )

    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_convert_insufficient_cp_returns_400(monkeypatch):
    monkeypatch.setattr(telegram_client, "get_chat_member_status", AsyncMock(return_value="member"))
    user_id = 400108
    await _ensure_user(user_id)
    init_data = _build_init_data(user_id=user_id)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/farm/convert",
            params={"chat_id": CONVERT_CHAT_ID},
            headers={"X-Telegram-Init-Data": init_data},
            json={"cp_in": 5_000_000, "ref_id": str(uuid.uuid4())},
        )

    assert resp.status_code == 400


# --- GET /farm/market ------------------------------------------------------


@pytest.mark.asyncio
async def test_get_farm_market_returns_price_and_history(monkeypatch):
    monkeypatch.setattr(telegram_client, "get_chat_member_status", AsyncMock(return_value="member"))
    user_id = 400109
    await _ensure_user(user_id)
    init_data = _build_init_data(user_id=user_id)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            "/api/v1/farm/market",
            params={"chat_id": CHAT_ID},
            headers={"X-Telegram-Init-Data": init_data},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert "price" in body
    assert "history" in body


# --- IDOR (T-04.2-02): user_id/chat_id ONLY from AuthContext --------------


@pytest.mark.asyncio
async def test_tap_ignores_foreign_user_id_in_body_idor(monkeypatch):
    monkeypatch.setattr(telegram_client, "get_chat_member_status", AsyncMock(return_value="member"))
    attacker_id = 400110
    victim_id = 400111
    await _ensure_user(attacker_id)
    await _ensure_user(victim_id)
    init_data = _build_init_data(user_id=attacker_id)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/farm/tap",
            params={"chat_id": CHAT_ID},
            headers={"X-Telegram-Init-Data": init_data},
            json={"count": 3, "elapsed_ms": 1000, "user_id": victim_id},
        )

    assert resp.status_code == 200

    async with SessionLocal() as verify_session:
        victim_farm = (
            await verify_session.execute(
                update(ClickerFarm)
                .where(ClickerFarm.chat_id == CHAT_ID, ClickerFarm.user_id == victim_id)
                .values(cp=ClickerFarm.cp)  # no-op update, just to check existence below
                .returning(ClickerFarm.cp)
            )
        ).scalar_one_or_none()
    assert victim_farm is None  # victim's farm row was never created/touched
