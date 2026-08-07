"""Тесты POST /api/v1/autobattle (create/accept/decline/cancel) + GET
/api/v1/autobattle/power (`api/routes/autobattle.py`, GACHA-06) — против
живого Postgres, тот же initData/`_app_state`-хелпер, что и
`tests/test_api_duel.py` (читай его докстринг за полным разбором конвенций —
этот файл зеркалит его структуру для autobattle).

В отличие от Duel здесь нет мут-пути (`api.duel_mute`) — accept_autobattle
не резолвит `mute_seconds`, поэтому нет аналога `test_accept_duel_resolves_
and_mutes_loser`/`test_accept_duel_mute_failure_does_not_break_response`.

IDOR (T-04.2-02): challenger/opponent/actor identity ВСЕГДА берётся из
`AuthContext` — ни один Pydantic-body-класс роута не несёт поле
challenger_id/opponent_id/actor_id (кроме `opponent_id` в
`CreateAutoBattleBody`, которое означает "кого вызываю", а не подмену
личности действующего пользователя), а `GET /power` читает только
`auth.user_id` (нет query-параметра user_id вовсе).
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
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from api import telegram_client
from api.main import app
from bot.config import settings
from bot.services import autobattle_service
from bot.services import economy_service
from common.db.session import engine
from common.db.session import SessionLocal
from common.models.autobattle import AutoBattle
from common.models.gacha_collection import GachaCollection
from common.models.user import User

CREATE_CHAT_ID = -900411
ACCEPT_CHAT_ID = -900412
DECLINE_CHAT_ID = -900413
CANCEL_CHAT_ID = -900414
POWER_CHAT_ID = -900415


class _ForcedRandomRng:
    """Тестовый RNG-стаб (форма `test_autobattle_service.py::
    _ForcedRandomRng`), monkeypatched вместо `autobattle_service._rng` —
    форсирует детерминированное `.random()` вместо реальной случайности."""

    def __init__(self, value: float):
        self._value = value

    def random(self) -> float:
        return self._value


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


async def _topup(chat_id: int, user_id: int, min_balance: int = 1000) -> None:
    """Тот же Rule-1 фикс, что `test_api_duel.py::_topup` — фиксированные
    user_id-литералы этого файла против ДОЛГОЖИВУЩЕГО docker-compose
    Postgres-контейнера истощаются накопительными списаниями ставок по мере
    повторных прогонов полного набора тестов."""
    async with SessionLocal() as db_session:
        balance = await economy_service.get_balance(db_session, chat_id, user_id)
        if balance < min_balance:
            await economy_service.credit(
                db_session,
                chat_id,
                user_id,
                min_balance - balance,
                kind="test_topup",
                ref_id=f"test_topup:{uuid.uuid4()}",
            )
            await db_session.commit()


async def _get_autobattle(autobattle_id: int) -> AutoBattle:
    async with SessionLocal() as db_session:
        return (
            await db_session.execute(select(AutoBattle).where(AutoBattle.id == autobattle_id))
        ).scalar_one()


async def _create_pending_autobattle(
    chat_id: int, challenger_id: int, opponent_id: int, stake: int = 20
) -> int:
    async with SessionLocal() as db_session:
        battle = await autobattle_service.create_autobattle(
            db_session, chat_id, challenger_id, opponent_id, stake, str(uuid.uuid4())
        )
        return battle.id


async def _seed_character(
    chat_id: int, user_id: int, char_id: str, stars: int = 1, farm_level: int = 1
) -> None:
    """Upsert по (user_id, chat_id, char_id) — та же необходимость, что
    `_topup` (Rule-1 фикс, ДОЛГОЖИВУЩИЙ docker-compose Postgres против
    фиксированных user_id-литералов этого файла между повторными прогонами)."""
    async with SessionLocal() as db_session:
        stmt = (
            pg_insert(GachaCollection)
            .values(
                chat_id=chat_id,
                user_id=user_id,
                char_id=char_id,
                stars=stars,
                farm_level=farm_level,
                copies=1,
            )
            .on_conflict_do_update(
                constraint="uq_gacha_collection_user_chat_char",
                set_={"stars": stars, "farm_level": farm_level},
            )
        )
        await db_session.execute(stmt)
        await db_session.commit()


@pytest.fixture(autouse=True)
def _reset_membership_cache():
    telegram_client.reset_cache()
    yield
    telegram_client.reset_cache()


@pytest.fixture(autouse=True)
async def _fresh_engine_per_test():
    """См. `test_api_duel.py::_fresh_engine_per_test` — тот же
    процесс-глобальный engine, та же необходимость `dispose()` между
    тестами с разными event loop'ами (pytest-asyncio, function-scoped loop)."""
    await engine.dispose()
    yield
    await engine.dispose()


@pytest.fixture(autouse=True)
def _app_state():
    app.state.http_client = AsyncMock()
    app.state.redis = None
    yield


# --- POST /api/v1/autobattle (create) ----------------------------------------


@pytest.mark.asyncio
async def test_create_autobattle_valid_returns_200(monkeypatch):
    monkeypatch.setattr(telegram_client, "get_chat_member_status", AsyncMock(return_value="member"))
    challenger_id, opponent_id = 941101, 941102
    await _ensure_user(challenger_id)
    await _ensure_user(opponent_id)
    await _topup(CREATE_CHAT_ID, challenger_id)
    init_data = _build_init_data(user_id=challenger_id)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/autobattle",
            params={"chat_id": CREATE_CHAT_ID},
            headers={"X-Telegram-Init-Data": init_data},
            json={"opponent_id": opponent_id, "stake": 20, "ref_id": str(uuid.uuid4())},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "pending"
    assert "autobattle_id" in body

    battle = await _get_autobattle(body["autobattle_id"])
    assert battle.challenger_id == challenger_id
    assert battle.opponent_id == opponent_id


@pytest.mark.asyncio
async def test_create_autobattle_missing_init_data_returns_401():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/autobattle",
            params={"chat_id": CREATE_CHAT_ID},
            json={"opponent_id": 941103, "stake": 20, "ref_id": str(uuid.uuid4())},
        )

    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_create_autobattle_stake_below_minimum_returns_400(monkeypatch):
    monkeypatch.setattr(telegram_client, "get_chat_member_status", AsyncMock(return_value="member"))
    challenger_id = 941104
    await _ensure_user(challenger_id)
    init_data = _build_init_data(user_id=challenger_id)
    assert 1 < settings.casino_min_bet  # гарантирует, что stake=1 реально ниже минимума

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/autobattle",
            params={"chat_id": CREATE_CHAT_ID},
            headers={"X-Telegram-Init-Data": init_data},
            json={"opponent_id": 941105, "stake": 1, "ref_id": str(uuid.uuid4())},
        )

    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_create_autobattle_replay_same_ref_id_returns_400(monkeypatch):
    """AutoBattleAlreadyResolved (ref_id уже применён economy_service.debit)
    — тот же AutoBattleError-подкласс, маппится на 400."""
    monkeypatch.setattr(telegram_client, "get_chat_member_status", AsyncMock(return_value="member"))
    challenger_id, opponent_id = 941106, 941107
    await _ensure_user(challenger_id)
    await _ensure_user(opponent_id)
    await _topup(CREATE_CHAT_ID, challenger_id)
    init_data = _build_init_data(user_id=challenger_id)
    ref_id = str(uuid.uuid4())

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        first = await client.post(
            "/api/v1/autobattle",
            params={"chat_id": CREATE_CHAT_ID},
            headers={"X-Telegram-Init-Data": init_data},
            json={"opponent_id": opponent_id, "stake": 20, "ref_id": ref_id},
        )
        second = await client.post(
            "/api/v1/autobattle",
            params={"chat_id": CREATE_CHAT_ID},
            headers={"X-Telegram-Init-Data": init_data},
            json={"opponent_id": opponent_id, "stake": 20, "ref_id": ref_id},
        )

    assert first.status_code == 200
    assert second.status_code == 400


@pytest.mark.asyncio
async def test_create_autobattle_self_challenge_returns_400(monkeypatch):
    """WR-01 (по образцу Duel): вызов самого себя через Mini App API
    отвергается сервисным слоем, не только хендлером бота."""
    monkeypatch.setattr(telegram_client, "get_chat_member_status", AsyncMock(return_value="member"))
    challenger_id = 941110
    await _ensure_user(challenger_id)
    init_data = _build_init_data(user_id=challenger_id)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/autobattle",
            params={"chat_id": CREATE_CHAT_ID},
            headers={"X-Telegram-Init-Data": init_data},
            json={"opponent_id": challenger_id, "stake": 20, "ref_id": str(uuid.uuid4())},
        )

    assert resp.status_code == 400
    async with SessionLocal() as db_session:
        count = (
            await db_session.execute(
                select(AutoBattle).where(
                    AutoBattle.chat_id == CREATE_CHAT_ID, AutoBattle.challenger_id == challenger_id
                )
            )
        ).scalars().all()
    assert count == []  # ставка не должна была списаться / автобитва не создана


@pytest.mark.asyncio
async def test_create_autobattle_ignores_challenger_id_in_body_idor(monkeypatch):
    """IDOR: CreateAutoBattleBody не содержит challenger_id вовсе —
    поддельное поле в JSON молча игнорируется, автобитва всегда создаётся от
    лица AuthContext.user_id, а не "жертвы" из тела запроса."""
    monkeypatch.setattr(telegram_client, "get_chat_member_status", AsyncMock(return_value="member"))
    attacker_id, victim_id, opponent_id = 941108, 941109, 941110
    await _ensure_user(attacker_id)
    await _ensure_user(victim_id)
    await _ensure_user(opponent_id)
    await _topup(CREATE_CHAT_ID, attacker_id)
    await _topup(CREATE_CHAT_ID, victim_id)
    init_data = _build_init_data(user_id=attacker_id)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/autobattle",
            params={"chat_id": CREATE_CHAT_ID},
            headers={"X-Telegram-Init-Data": init_data},
            json={
                "opponent_id": opponent_id,
                "stake": 20,
                "ref_id": str(uuid.uuid4()),
                "challenger_id": victim_id,  # атакующий пытается подставить чужой challenger_id
            },
        )

    assert resp.status_code == 200
    battle = await _get_autobattle(resp.json()["autobattle_id"])
    assert battle.challenger_id == attacker_id


# --- POST /api/v1/autobattle/{id}/accept --------------------------------------


@pytest.mark.asyncio
async def test_accept_autobattle_resolves_returns_200(monkeypatch):
    monkeypatch.setattr(telegram_client, "get_chat_member_status", AsyncMock(return_value="member"))
    challenger_id, opponent_id = 941201, 941202
    await _ensure_user(challenger_id)
    await _ensure_user(opponent_id)
    await _topup(ACCEPT_CHAT_ID, challenger_id)
    await _topup(ACCEPT_CHAT_ID, opponent_id)
    autobattle_id = await _create_pending_autobattle(ACCEPT_CHAT_ID, challenger_id, opponent_id)

    # Обе стороны без коллекции -> win_probability(0, 0) == 0.5; r=0.0 < 0.5
    # форсирует победу челленджера.
    monkeypatch.setattr(autobattle_service, "_rng", _ForcedRandomRng(0.0))

    init_data = _build_init_data(user_id=opponent_id)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            f"/api/v1/autobattle/{autobattle_id}/accept",
            params={"chat_id": ACCEPT_CHAT_ID},
            headers={"X-Telegram-Init-Data": init_data},
            json={"ref_id": str(uuid.uuid4())},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "resolved"
    assert body["winner_id"] == challenger_id
    assert body["loser_id"] == opponent_id
    assert "mute_seconds" not in body  # автобитва не резолвит мут — не Duel


@pytest.mark.asyncio
async def test_accept_autobattle_by_non_invited_user_returns_400_idor(monkeypatch):
    """IDOR: только приглашённый opponent_id (из AutoBattle-строки) может
    принять — AcceptAutoBattleBody не несёт opponent_id вовсе, identity
    приходит ТОЛЬКО из AuthContext."""
    monkeypatch.setattr(telegram_client, "get_chat_member_status", AsyncMock(return_value="member"))
    challenger_id, opponent_id, stranger_id = 941205, 941206, 941207
    await _ensure_user(challenger_id)
    await _ensure_user(opponent_id)
    await _ensure_user(stranger_id)
    await _topup(ACCEPT_CHAT_ID, challenger_id)
    autobattle_id = await _create_pending_autobattle(ACCEPT_CHAT_ID, challenger_id, opponent_id)

    init_data = _build_init_data(user_id=stranger_id)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            f"/api/v1/autobattle/{autobattle_id}/accept",
            params={"chat_id": ACCEPT_CHAT_ID},
            headers={"X-Telegram-Init-Data": init_data},
            json={"ref_id": str(uuid.uuid4())},
        )

    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_accept_autobattle_not_found_returns_404(monkeypatch):
    monkeypatch.setattr(telegram_client, "get_chat_member_status", AsyncMock(return_value="member"))
    user_id = 941208
    await _ensure_user(user_id)
    init_data = _build_init_data(user_id=user_id)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/autobattle/999999999/accept",
            params={"chat_id": ACCEPT_CHAT_ID},
            headers={"X-Telegram-Init-Data": init_data},
            json={"ref_id": str(uuid.uuid4())},
        )

    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_accept_autobattle_outcome_reflects_power(monkeypatch):
    """Не просто "резолвится" — победитель реально определяется взвешенной
    силой коллекции (не честной монеткой), сквозь весь HTTP-стек."""
    monkeypatch.setattr(telegram_client, "get_chat_member_status", AsyncMock(return_value="member"))
    challenger_id, opponent_id = 941209, 941210
    await _ensure_user(challenger_id)
    await _ensure_user(opponent_id)
    await _topup(ACCEPT_CHAT_ID, challenger_id)
    await _topup(ACCEPT_CHAT_ID, opponent_id)
    await _seed_character(ACCEPT_CHAT_ID, challenger_id, "uur_astrea", stars=5, farm_level=1)
    autobattle_id = await _create_pending_autobattle(ACCEPT_CHAT_ID, challenger_id, opponent_id)

    # Челленджер сильно доминирует по силе -> win_probability зажата на 0.95;
    # r чуть выше порога форсирует редкий, но валидный проигрыш фаворита.
    monkeypatch.setattr(autobattle_service, "_rng", _ForcedRandomRng(0.99))

    init_data = _build_init_data(user_id=opponent_id)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            f"/api/v1/autobattle/{autobattle_id}/accept",
            params={"chat_id": ACCEPT_CHAT_ID},
            headers={"X-Telegram-Init-Data": init_data},
            json={"ref_id": str(uuid.uuid4())},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["winner_id"] == opponent_id
    assert body["challenger_power"] > 0
    assert body["opponent_power"] == 0


# --- POST /api/v1/autobattle/{id}/decline + /cancel ---------------------------


@pytest.mark.asyncio
async def test_decline_autobattle_by_opponent_returns_declined(monkeypatch):
    monkeypatch.setattr(telegram_client, "get_chat_member_status", AsyncMock(return_value="member"))
    challenger_id, opponent_id = 941301, 941302
    await _ensure_user(challenger_id)
    await _ensure_user(opponent_id)
    await _topup(DECLINE_CHAT_ID, challenger_id)
    autobattle_id = await _create_pending_autobattle(DECLINE_CHAT_ID, challenger_id, opponent_id)

    init_data = _build_init_data(user_id=opponent_id)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            f"/api/v1/autobattle/{autobattle_id}/decline",
            params={"chat_id": DECLINE_CHAT_ID},
            headers={"X-Telegram-Init-Data": init_data},
        )

    assert resp.status_code == 200
    assert resp.json()["status"] == "declined"


@pytest.mark.asyncio
async def test_decline_autobattle_by_non_opponent_returns_400_idor(monkeypatch):
    monkeypatch.setattr(telegram_client, "get_chat_member_status", AsyncMock(return_value="member"))
    challenger_id, opponent_id = 941303, 941304
    await _ensure_user(challenger_id)
    await _ensure_user(opponent_id)
    await _topup(DECLINE_CHAT_ID, challenger_id)
    autobattle_id = await _create_pending_autobattle(DECLINE_CHAT_ID, challenger_id, opponent_id)

    # challenger пытается "отклонить" свой же вызов — только opponent_id может.
    init_data = _build_init_data(user_id=challenger_id)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            f"/api/v1/autobattle/{autobattle_id}/decline",
            params={"chat_id": DECLINE_CHAT_ID},
            headers={"X-Telegram-Init-Data": init_data},
        )

    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_cancel_autobattle_by_challenger_returns_cancelled(monkeypatch):
    monkeypatch.setattr(telegram_client, "get_chat_member_status", AsyncMock(return_value="member"))
    challenger_id, opponent_id = 941401, 941402
    await _ensure_user(challenger_id)
    await _ensure_user(opponent_id)
    await _topup(CANCEL_CHAT_ID, challenger_id)
    autobattle_id = await _create_pending_autobattle(CANCEL_CHAT_ID, challenger_id, opponent_id)

    init_data = _build_init_data(user_id=challenger_id)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            f"/api/v1/autobattle/{autobattle_id}/cancel",
            params={"chat_id": CANCEL_CHAT_ID},
            headers={"X-Telegram-Init-Data": init_data},
        )

    assert resp.status_code == 200
    assert resp.json()["status"] == "cancelled"


@pytest.mark.asyncio
async def test_cancel_autobattle_by_non_challenger_returns_400_idor(monkeypatch):
    monkeypatch.setattr(telegram_client, "get_chat_member_status", AsyncMock(return_value="member"))
    challenger_id, opponent_id = 941403, 941404
    await _ensure_user(challenger_id)
    await _ensure_user(opponent_id)
    await _topup(CANCEL_CHAT_ID, challenger_id)
    autobattle_id = await _create_pending_autobattle(CANCEL_CHAT_ID, challenger_id, opponent_id)

    init_data = _build_init_data(user_id=opponent_id)  # оппонент, не челленджер

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            f"/api/v1/autobattle/{autobattle_id}/cancel",
            params={"chat_id": CANCEL_CHAT_ID},
            headers={"X-Telegram-Init-Data": init_data},
        )

    assert resp.status_code == 400


# --- GET /api/v1/autobattle/power ---------------------------------------------


@pytest.mark.asyncio
async def test_get_power_returns_own_team_power(monkeypatch):
    monkeypatch.setattr(telegram_client, "get_chat_member_status", AsyncMock(return_value="member"))
    user_id = 941501
    await _ensure_user(user_id)
    await _seed_character(POWER_CHAT_ID, user_id, "r_elis", stars=1, farm_level=1)
    init_data = _build_init_data(user_id=user_id)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            "/api/v1/autobattle/power",
            params={"chat_id": POWER_CHAT_ID},
            headers={"X-Telegram-Init-Data": init_data},
        )

    assert resp.status_code == 200
    assert resp.json()["power"] == 10  # R-тир, 1 звезда, farm_level 1 -> TIER_POWER["R"]


@pytest.mark.asyncio
async def test_get_power_reads_only_own_collection_not_other_users(monkeypatch):
    """Нет user_id-параметра на роуте — GET /power не может прочитать чужую
    силу, только AuthContext.user_id."""
    monkeypatch.setattr(telegram_client, "get_chat_member_status", AsyncMock(return_value="member"))
    caller_id, other_id = 941502, 941503
    await _ensure_user(caller_id)
    await _ensure_user(other_id)
    # Только "чужой" юзер собрал персонажей — вызывающий пуст.
    await _seed_character(POWER_CHAT_ID, other_id, "uur_astrea", stars=5, farm_level=1)
    init_data = _build_init_data(user_id=caller_id)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            "/api/v1/autobattle/power",
            params={"chat_id": POWER_CHAT_ID, "user_id": other_id},  # игнорируется, если бы был
            headers={"X-Telegram-Init-Data": init_data},
        )

    assert resp.status_code == 200
    assert resp.json()["power"] == 0


@pytest.mark.asyncio
async def test_get_power_missing_init_data_returns_401():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/v1/autobattle/power", params={"chat_id": POWER_CHAT_ID})

    assert resp.status_code == 401
