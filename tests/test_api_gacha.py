"""Тесты POST /api/v1/gacha/roll + GET /api/v1/gacha/collection (Task 1/2,
api/routes/gacha.py) — против живого Postgres. Тот же initData-хелпер/
фикстуры, что и `test_api_games.py`/`test_api_farm.py` (независимая от
`api/deps.py` сборка initData, monkeypatch членства, `engine.dispose()`
между тестами из-за pytest-asyncio function-scoped loop).

Роуты — тонкая обёртка над `gacha_service` (уже полностью протестирован
против pity/rate-up/дублей/идемпотентности в `test_gacha_service.py` Фазы
04.1) — здесь проверяется только HTTP-контракт: auth (401 без initData),
маппинг `GachaError` -> 400 (count не в {1,10}), IDOR (T-04.2-02), и что
GET /gacha/collection реально приходит через `gacha_service.get_collection`
(SELECT), а не пишет свой SQL прямо в роуте.
"""

from __future__ import annotations

import hashlib
import hmac
import inspect
import json
import time
import uuid
from unittest.mock import AsyncMock
from urllib.parse import urlencode

import pytest
from httpx import ASGITransport
from httpx import AsyncClient

from api import telegram_client
from api.main import app
from api.routes import gacha as gacha_route
from bot.config import settings
from bot.services import economy_service
from bot.services import gacha_catalog
from bot.services import gacha_service
from common.db.session import engine
from common.db.session import SessionLocal
from common.models.user import User
from sqlalchemy.dialects.postgresql import insert as pg_insert

CHAT_ID = -900501


class _ForcedRng:
    """Тот же тестовый RNG-стаб, что `test_gacha_service.py::_ForcedRng`
    (monkeypatched вместо `gacha_service._rng`) — `random()` форсирует
    взвешенный выбор тира, `choice(seq)` форсирует выбор персонажа."""

    def __init__(self, random_value: float = 0.0, choice_index: int = 0, cycle: bool = False):
        self._random_value = random_value
        self._choice_index = choice_index
        self._cycle = cycle
        self._call_count = 0

    def random(self) -> float:
        return self._random_value

    def choice(self, seq):
        index = (self._choice_index + self._call_count) if self._cycle else self._choice_index
        self._call_count += 1
        return seq[index % len(seq)]


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


async def _get_balance(chat_id: int, user_id: int) -> int:
    async with SessionLocal() as db_session:
        return await economy_service.get_balance(db_session, chat_id, user_id)


async def _top_up(chat_id: int, user_id: int, amount: int, ref_id: str) -> None:
    async with SessionLocal() as db_session:
        await economy_service.get_balance(db_session, chat_id, user_id)
        await economy_service.credit(
            db_session, chat_id, user_id, amount, kind="test_top_up", ref_id=ref_id
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


# --- POST /api/v1/gacha/roll ------------------------------------------------


@pytest.mark.asyncio
async def test_roll_count1_valid_returns_200(monkeypatch):
    monkeypatch.setattr(telegram_client, "get_chat_member_status", AsyncMock(return_value="member"))
    monkeypatch.setattr(gacha_service, "_rng", _ForcedRng(random_value=0.0, choice_index=0))
    user_id = 500201
    await _ensure_user(user_id)
    # Top-up с уникальным ref_id на каждый запуск (не фиксированным) —
    # HTTP-тесты коммитят напрямую в живой Postgres (в отличие от
    # `session`-фикстуры conftest.py), повторные прогоны набора тестов
    # против того же контейнера иначе постепенно исчерпали бы фиксированный
    # стартовый бонус (economy_start_bonus=1000) этого user_id, т.к.
    # каждый прогон тратит реальные ювики новым (случайным) ref_id ролла
    # (тот же класс non-determinism, что уже найден и исправлен в
    # test_api_farm.py::_seed_farm_cp, 04.2-04).
    await _top_up(CHAT_ID, user_id, 5000, str(uuid.uuid4()))
    init_data = _build_init_data(user_id=user_id)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/gacha/roll",
            params={"chat_id": CHAT_ID},
            headers={"X-Telegram-Init-Data": init_data},
            json={"count": 1, "ref_id": str(uuid.uuid4())},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["cost"] == gacha_service.ROLL_COST == 300
    assert len(body["results"]) == 1
    assert "user_balance_after" in body


@pytest.mark.asyncio
async def test_roll_count10_valid_returns_200(monkeypatch):
    monkeypatch.setattr(telegram_client, "get_chat_member_status", AsyncMock(return_value="member"))
    monkeypatch.setattr(gacha_service, "_rng", _ForcedRng(random_value=0.0, cycle=True))
    user_id = 500202
    await _ensure_user(user_id)
    await _top_up(CHAT_ID, user_id, 5000, str(uuid.uuid4()))  # see comment above (unique per run)
    init_data = _build_init_data(user_id=user_id)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/gacha/roll",
            params={"chat_id": CHAT_ID},
            headers={"X-Telegram-Init-Data": init_data},
            json={"count": 10, "ref_id": str(uuid.uuid4())},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["cost"] == gacha_service.ROLL10_COST == 2700
    assert len(body["results"]) == 10


@pytest.mark.asyncio
async def test_roll_invalid_count_returns_400(monkeypatch):
    monkeypatch.setattr(telegram_client, "get_chat_member_status", AsyncMock(return_value="member"))
    user_id = 500203
    await _ensure_user(user_id)
    init_data = _build_init_data(user_id=user_id)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/gacha/roll",
            params={"chat_id": CHAT_ID},
            headers={"X-Telegram-Init-Data": init_data},
            json={"count": 5, "ref_id": str(uuid.uuid4())},
        )

    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_roll_missing_init_data_returns_401():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/gacha/roll",
            params={"chat_id": CHAT_ID},
            json={"count": 1, "ref_id": str(uuid.uuid4())},
        )

    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_roll_replay_same_ref_id_is_idempotent(monkeypatch):
    monkeypatch.setattr(telegram_client, "get_chat_member_status", AsyncMock(return_value="member"))
    monkeypatch.setattr(gacha_service, "_rng", _ForcedRng(random_value=0.0, choice_index=0))
    user_id = 500204
    await _ensure_user(user_id)
    await _top_up(CHAT_ID, user_id, 5000, str(uuid.uuid4()))  # see comment above (unique per run)
    init_data = _build_init_data(user_id=user_id)
    ref_id = str(uuid.uuid4())

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        first = await client.post(
            "/api/v1/gacha/roll",
            params={"chat_id": CHAT_ID},
            headers={"X-Telegram-Init-Data": init_data},
            json={"count": 1, "ref_id": ref_id},
        )
        balance_after_first = first.json()["user_balance_after"]

        second = await client.post(
            "/api/v1/gacha/roll",
            params={"chat_id": CHAT_ID},
            headers={"X-Telegram-Init-Data": init_data},
            json={"count": 1, "ref_id": ref_id},
        )

    assert second.status_code == 200
    body = second.json()
    assert body["replay"] is True
    assert body["results"] == []
    assert body["user_balance_after"] == balance_after_first


@pytest.mark.asyncio
async def test_roll_ignores_foreign_user_id_in_body_idor(monkeypatch):
    """T-04.2-02: та же IDOR-защита, что и у games.py/farm.py — поддельный
    user_id "жертвы" в теле запроса не должен сдвинуть её баланс."""
    monkeypatch.setattr(telegram_client, "get_chat_member_status", AsyncMock(return_value="member"))
    monkeypatch.setattr(gacha_service, "_rng", _ForcedRng(random_value=0.0, choice_index=0))
    attacker_id = 500205
    victim_id = 500206
    await _ensure_user(attacker_id)
    await _ensure_user(victim_id)
    await _top_up(CHAT_ID, attacker_id, 5000, str(uuid.uuid4()))  # unique per run, see above
    init_data = _build_init_data(user_id=attacker_id)

    victim_before = await _get_balance(CHAT_ID, victim_id)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/gacha/roll",
            params={"chat_id": CHAT_ID},
            headers={"X-Telegram-Init-Data": init_data},
            json={
                "count": 1,
                "ref_id": str(uuid.uuid4()),
                "user_id": victim_id,  # атакующий пытается подставить чужой user_id
            },
        )

    assert resp.status_code == 200

    victim_after = await _get_balance(CHAT_ID, victim_id)
    assert victim_after == victim_before  # жертва не затронута вовсе


# --- GET /api/v1/gacha/collection -------------------------------------------


@pytest.mark.asyncio
async def test_get_collection_empty_for_user_with_no_gacha(monkeypatch):
    monkeypatch.setattr(telegram_client, "get_chat_member_status", AsyncMock(return_value="member"))
    user_id = 500301
    await _ensure_user(user_id)
    init_data = _build_init_data(user_id=user_id)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            "/api/v1/gacha/collection",
            params={"chat_id": CHAT_ID},
            headers={"X-Telegram-Init-Data": init_data},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["characters"] == []
    assert "pity_ssr" in body
    assert "pity_ur" in body
    assert "banner" in body


@pytest.mark.asyncio
async def test_get_collection_returns_owned_characters_enriched(monkeypatch):
    monkeypatch.setattr(telegram_client, "get_chat_member_status", AsyncMock(return_value="member"))
    monkeypatch.setattr(gacha_service, "_rng", _ForcedRng(random_value=0.0, choice_index=0))
    user_id = 500302
    await _ensure_user(user_id)
    init_data = _build_init_data(user_id=user_id)

    async with SessionLocal() as db_session:
        await gacha_service.roll(db_session, CHAT_ID, user_id, 1, "test_collection_seed")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            "/api/v1/gacha/collection",
            params={"chat_id": CHAT_ID},
            headers={"X-Telegram-Init-Data": init_data},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert len(body["characters"]) == 1
    char = body["characters"][0]
    expected = gacha_catalog.chars_of_tier("SR")[0]
    assert char["char_id"] == expected.char_id
    assert char["name"] == expected.name
    assert char["tier"] == "SR"
    assert char["role"] == expected.role
    assert char["stars"] == 1
    assert char["copies"] == 1


@pytest.mark.asyncio
async def test_get_collection_missing_init_data_returns_401():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/v1/gacha/collection", params={"chat_id": CHAT_ID})

    assert resp.status_code == 401


def test_gacha_route_composes_service_no_raw_sql():
    """must_haves: "The collection read path composes gacha_service +
    gacha_catalog (no direct SQL in the route)" — источник роута не должен
    содержать ни SELECT-запросов, ни прямой ссылки на ORM-модель
    GachaCollection (это ответственность gacha_service.get_collection)."""
    source = inspect.getsource(gacha_route)
    assert "select(" not in source
    assert "GachaCollection" not in source
