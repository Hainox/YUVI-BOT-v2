"""Тесты GET /api/v1/stats (D-05, план 04.2-09) — против живого Postgres.

initData собирается тем же алгоритмом, что `test_api_economy.py::_build_init_data`
(независимая сборка = доказательство, что `require_membership` реально
проверяет подпись, а не пропускает всё). `telegram_client.
get_chat_member_status` монкипатчится (без сети к Telegram) — membership
уже покрыт `test_api_auth.py`.

Роут компонует уже существующие read-функции `economy_service`/
`stats_service`/`clicker_service` — эти тесты сидят реальные строки
(`casino_games`, `daily_stats`, `economy_tx`) напрямую через SQLAlchemy-модели
и проверяют, что дашборд агрегирует их корректно, БЕЗ повторной денежной
логики (запись через `_settle`/`credit`/`debit` здесь не нужна — только
чтение).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from datetime import date
from datetime import timedelta
from unittest.mock import AsyncMock
from urllib.parse import urlencode

import pytest
from httpx import ASGITransport
from httpx import AsyncClient
from sqlalchemy.dialects.postgresql import insert as pg_insert

from api import telegram_client
from api.main import app
from bot.config import settings
from bot.services import economy_service
from common.db.session import engine
from common.db.session import SessionLocal
from common.models.casino_game import CasinoGame
from common.models.daily_stat import DailyStat
from common.models.economy_tx import EconomyTx
from common.models.user import User

CHAT_ID = -900301


def _build_init_data(*, user_id: int, bot_token: str | None = None, tamper: bool = False) -> str:
    """Строит валидный (или намеренно испорченный) initData — форма
    `test_api_economy.py::_build_init_data`, независимая от `api/deps.py`."""
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


async def _seed_casino_round(
    chat_id: int, user_id: int, game: str, bet: int, payout: int, idem_key: str
) -> None:
    async with SessionLocal() as db_session:
        db_session.add(
            CasinoGame(
                chat_id=chat_id,
                user_id=user_id,
                game=game,
                bet=bet,
                payout=payout,
                outcome={"seeded": True},
                status="settled",
                idem_key=idem_key,
            )
        )
        await db_session.commit()


async def _seed_daily_stat(chat_id: int, user_id: int, stat_date: date, message_count: int) -> None:
    async with SessionLocal() as db_session:
        stmt = (
            pg_insert(DailyStat)
            .values(chat_id=chat_id, user_id=user_id, stat_date=stat_date, message_count=message_count)
            .on_conflict_do_update(
                index_elements=["chat_id", "user_id", "stat_date"],
                set_={"message_count": message_count},
            )
        )
        await db_session.execute(stmt)
        await db_session.commit()


async def _seed_farm_convert_tx(chat_id: int, user_id: int, amount: int, ref_id: str) -> None:
    async with SessionLocal() as db_session:
        db_session.add(
            EconomyTx(chat_id=chat_id, user_id=user_id, amount=amount, kind="farm_convert", ref_id=ref_id)
        )
        await db_session.commit()


async def _get_balance(chat_id: int, user_id: int) -> int:
    async with SessionLocal() as db_session:
        return await economy_service.get_balance(db_session, chat_id, user_id)


@pytest.fixture(autouse=True)
def _reset_membership_cache():
    telegram_client.reset_cache()
    yield
    telegram_client.reset_cache()


@pytest.fixture(autouse=True)
async def _fresh_engine_per_test():
    """Форма `test_api_economy.py::_fresh_engine_per_test` — каждый тест
    получает свой event loop, asyncpg-соединения из предыдущего loop'а
    должны быть уничтожены до/после теста."""
    await engine.dispose()
    yield
    await engine.dispose()


@pytest.fixture(autouse=True)
def _app_state():
    app.state.http_client = AsyncMock()
    app.state.redis = None
    yield


@pytest.mark.asyncio
async def test_stats_dashboard_composes_all_sections_for_active_user(monkeypatch):
    """Активный пользователь: все 4 секции дашборда заполнены реальными
    данными из casino_games/daily_stats/economy_tx/clicker_farms."""
    monkeypatch.setattr(telegram_client, "get_chat_member_status", AsyncMock(return_value="member"))
    chat_id = CHAT_ID
    user_id = 222201
    other_id = 222202
    await _ensure_user(user_id)
    await _ensure_user(other_id)
    balance = await _get_balance(chat_id, user_id)
    await _get_balance(chat_id, other_id)

    # Игровая статистика: 2 раунда, один проигрыш, один крупный выигрыш.
    await _seed_casino_round(chat_id, user_id, "coinflip", bet=100, payout=0, idem_key="stats-test-1")
    await _seed_casino_round(chat_id, user_id, "dice", bet=100, payout=500, idem_key="stats-test-2")

    # Активность чата: 3-дневная серия, включая сегодня.
    today = date.today()
    await _seed_daily_stat(chat_id, user_id, today, 10)
    await _seed_daily_stat(chat_id, user_id, today - timedelta(days=1), 5)
    await _seed_daily_stat(chat_id, user_id, today - timedelta(days=2), 5)
    # other_id набирает больше сообщений, чтобы у user_id был предсказуемый ранг > 1.
    await _seed_daily_stat(chat_id, other_id, today, 1000)

    # Ферма: конвертация CP в ювики.
    await _seed_farm_convert_tx(chat_id, user_id, 42, ref_id="stats-test-farm-1")

    init_data = _build_init_data(user_id=user_id)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            "/api/v1/stats",
            params={"chat_id": chat_id},
            headers={"X-Telegram-Init-Data": init_data},
        )

    assert resp.status_code == 200
    body = resp.json()

    # Баланс и банк
    assert body["balance"] == balance
    assert "bank_share_pct" in body

    # Игровая статистика — не дублирует SQL, композирует существующие данные.
    casino = body["casino"]
    assert casino["rounds_played"] == 2
    assert casino["net_result"] == (0 - 100) + (500 - 100)
    assert casino["biggest_win"]["amount"] == 500
    assert casino["biggest_win"]["game"] == "dice"

    # Активность чата
    activity = body["activity"]
    assert activity["streak"] == 3
    assert activity["peak_day"] is not None
    assert activity["message_rank"] == 2  # other_id набрал больше сообщений сегодня

    # Ферма
    farm = body["farm"]
    assert "cp_per_sec" in farm
    assert farm["total_converted"] == 42


@pytest.mark.asyncio
async def test_stats_dashboard_returns_nulls_not_errors_for_brand_new_user(monkeypatch):
    """Пользователь без единого раунда/сообщения не должен вызывать 500 —
    поля рендерятся как null (фронтенд покажет `—`), не ошибка."""
    monkeypatch.setattr(telegram_client, "get_chat_member_status", AsyncMock(return_value="member"))
    chat_id = CHAT_ID
    user_id = 222203
    await _ensure_user(user_id)
    await _get_balance(chat_id, user_id)

    init_data = _build_init_data(user_id=user_id)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            "/api/v1/stats",
            params={"chat_id": chat_id},
            headers={"X-Telegram-Init-Data": init_data},
        )

    assert resp.status_code == 200
    body = resp.json()

    casino = body["casino"]
    assert casino["rounds_played"] == 0
    assert casino["net_result"] == 0
    assert casino["biggest_win"] is None

    activity = body["activity"]
    assert activity["streak"] == 0
    assert activity["message_rank"] is None

    assert body["farm"]["total_converted"] == 0


@pytest.mark.asyncio
async def test_stats_dashboard_unauthenticated_returns_401():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/v1/stats", params={"chat_id": CHAT_ID})

    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_stats_route_delegates_to_existing_services_no_new_sql(monkeypatch):
    """T-04.2 compose-don't-duplicate: the route must call the existing
    stats_service/economy_service/clicker_service read functions rather than
    re-deriving their aggregates from scratch."""
    from api.routes import stats as stats_route
    from bot.services import clicker_service
    from bot.services import economy_service as economy_service_module
    from bot.services import stats_service

    monkeypatch.setattr(telegram_client, "get_chat_member_status", AsyncMock(return_value="member"))
    chat_id = CHAT_ID
    user_id = 222204
    await _ensure_user(user_id)
    await _get_balance(chat_id, user_id)
    init_data = _build_init_data(user_id=user_id)

    calls: dict[str, int] = {}

    def _tracked(module, name):
        original = getattr(module, name)

        async def wrapper(*args, **kwargs):
            calls[name] = calls.get(name, 0) + 1
            return await original(*args, **kwargs)

        monkeypatch.setattr(module, name, wrapper)

    _tracked(economy_service_module, "get_balance")
    _tracked(economy_service_module, "get_chat_summary")
    _tracked(stats_service, "get_streak")
    _tracked(stats_service, "get_peak_day")
    _tracked(stats_service, "get_top_participants")
    _tracked(clicker_service, "get_farm_state")
    # stats.py imports these modules by reference — re-point its module-level
    # names at the wrapped versions too (module import happens once at
    # collection time via api/main.py router auto-discovery).
    monkeypatch.setattr(stats_route, "economy_service", economy_service_module)
    monkeypatch.setattr(stats_route, "stats_service", stats_service)
    monkeypatch.setattr(stats_route, "clicker_service", clicker_service)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            "/api/v1/stats",
            params={"chat_id": chat_id},
            headers={"X-Telegram-Init-Data": init_data},
        )

    assert resp.status_code == 200
    for name in (
        "get_balance",
        "get_chat_summary",
        "get_streak",
        "get_peak_day",
        "get_top_participants",
        "get_farm_state",
    ):
        assert calls.get(name, 0) >= 1, f"{name} was not called — route must compose existing reads"
