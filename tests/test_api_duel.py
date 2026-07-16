"""Тесты POST /api/v1/duel (create/accept/decline/cancel) + POST
/api/v1/duelbot (Task 1, `api/routes/duel.py`) — против живого Postgres,
тот же initData/`_app_state`-хелпер, что и `test_api_games.py`.

Мут-путь (`api/duel_mute.py::apply_mute_from_api`) мокается на уровне
`api.routes.duel.apply_mute_from_api` (импортирован ПО ИМЕНИ в модуль
роута — monkeypatch должен подменять именно эту привязку, а не оригинал в
`api.duel_mute`) — тесты никогда не ходят в реальный Telegram Bot API.
Проверяется: (1) accept вызывает мут для `loser_id`/`mute_seconds` из
результата `duel_service.accept_duel`; (2) исключение из мут-пути НЕ
ломает успешный ответ accept (деньги уже двинулись, WR-05-прецедент из
`bot/handlers/duel.py`).

IDOR (T-04.2-02): challenger/opponent/actor identity ВСЕГДА берётся из
`AuthContext` — ни один Pydantic-body-класс роута не несёт поле
challenger_id/opponent_id/actor_id (кроме `opponent_id` в `CreateDuelBody`,
которое означает "кого вызываю", а не подмену личности действующего
пользователя).
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

import api.routes.duel as duel_route
from api import telegram_client
from api.main import app
from bot.config import settings
from bot.services import duel_service
from common.db.session import engine
from common.db.session import SessionLocal
from common.models.duel import Duel
from common.models.user import User

CREATE_CHAT_ID = -900401
ACCEPT_CHAT_ID = -900402
DECLINE_CHAT_ID = -900403
CANCEL_CHAT_ID = -900404
DUELBOT_CHAT_ID = -900405


class _ForcedChoiceRng:
    """Тестовый RNG-стаб (форма `test_duel_service.py::_ForcedChoiceRng`),
    monkeypatched вместо `duel_service._rng` — форсирует детерминированный
    `.choice(seq)` вместо реальной случайности."""

    def __init__(self, choice_value):
        self._choice_value = choice_value

    def choice(self, seq):
        return self._choice_value


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


async def _get_duel(duel_id: int) -> Duel:
    async with SessionLocal() as db_session:
        return (await db_session.execute(select(Duel).where(Duel.id == duel_id))).scalar_one()


async def _create_pending_duel(chat_id: int, challenger_id: int, opponent_id: int, stake: int = 20) -> int:
    async with SessionLocal() as db_session:
        duel = await duel_service.create_duel(
            db_session, chat_id, challenger_id, opponent_id, stake, str(uuid.uuid4())
        )
        return duel.id


@pytest.fixture(autouse=True)
def _reset_membership_cache():
    telegram_client.reset_cache()
    yield
    telegram_client.reset_cache()


@pytest.fixture(autouse=True)
async def _fresh_engine_per_test():
    """См. `test_api_games.py::_fresh_engine_per_test` — тот же
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


# --- POST /api/v1/duel (create) ----------------------------------------------


@pytest.mark.asyncio
async def test_create_duel_valid_returns_200(monkeypatch):
    monkeypatch.setattr(telegram_client, "get_chat_member_status", AsyncMock(return_value="member"))
    challenger_id, opponent_id = 940101, 940102
    await _ensure_user(challenger_id)
    await _ensure_user(opponent_id)
    init_data = _build_init_data(user_id=challenger_id)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/duel",
            params={"chat_id": CREATE_CHAT_ID},
            headers={"X-Telegram-Init-Data": init_data},
            json={"opponent_id": opponent_id, "stake": 20, "ref_id": str(uuid.uuid4())},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "pending"
    assert "duel_id" in body

    duel = await _get_duel(body["duel_id"])
    assert duel.challenger_id == challenger_id
    assert duel.opponent_id == opponent_id


@pytest.mark.asyncio
async def test_create_duel_missing_init_data_returns_401():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/duel",
            params={"chat_id": CREATE_CHAT_ID},
            json={"opponent_id": 940103, "stake": 20, "ref_id": str(uuid.uuid4())},
        )

    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_create_duel_stake_below_minimum_returns_400(monkeypatch):
    monkeypatch.setattr(telegram_client, "get_chat_member_status", AsyncMock(return_value="member"))
    challenger_id = 940104
    await _ensure_user(challenger_id)
    init_data = _build_init_data(user_id=challenger_id)
    assert 1 < settings.casino_min_bet  # гарантирует, что stake=1 реально ниже минимума

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/duel",
            params={"chat_id": CREATE_CHAT_ID},
            headers={"X-Telegram-Init-Data": init_data},
            json={"opponent_id": 940105, "stake": 1, "ref_id": str(uuid.uuid4())},
        )

    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_create_duel_replay_same_ref_id_returns_400(monkeypatch):
    """DuelAlreadyResolved (ref_id уже применён economy_service.debit) — тот
    же DuelError-подкласс, маппится на 400."""
    monkeypatch.setattr(telegram_client, "get_chat_member_status", AsyncMock(return_value="member"))
    challenger_id, opponent_id = 940106, 940107
    await _ensure_user(challenger_id)
    await _ensure_user(opponent_id)
    init_data = _build_init_data(user_id=challenger_id)
    ref_id = str(uuid.uuid4())

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        first = await client.post(
            "/api/v1/duel",
            params={"chat_id": CREATE_CHAT_ID},
            headers={"X-Telegram-Init-Data": init_data},
            json={"opponent_id": opponent_id, "stake": 20, "ref_id": ref_id},
        )
        second = await client.post(
            "/api/v1/duel",
            params={"chat_id": CREATE_CHAT_ID},
            headers={"X-Telegram-Init-Data": init_data},
            json={"opponent_id": opponent_id, "stake": 20, "ref_id": ref_id},
        )

    assert first.status_code == 200
    assert second.status_code == 400


@pytest.mark.asyncio
async def test_create_duel_ignores_challenger_id_in_body_idor(monkeypatch):
    """IDOR: CreateDuelBody не содержит challenger_id вовсе — поддельное
    поле в JSON молча игнорируется, дуэль всегда создаётся от лица
    AuthContext.user_id, а не "жертвы" из тела запроса."""
    monkeypatch.setattr(telegram_client, "get_chat_member_status", AsyncMock(return_value="member"))
    attacker_id, victim_id, opponent_id = 940108, 940109, 940110
    await _ensure_user(attacker_id)
    await _ensure_user(victim_id)
    await _ensure_user(opponent_id)
    init_data = _build_init_data(user_id=attacker_id)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/duel",
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
    duel = await _get_duel(resp.json()["duel_id"])
    assert duel.challenger_id == attacker_id


# --- POST /api/v1/duel/{id}/accept -------------------------------------------


@pytest.mark.asyncio
async def test_accept_duel_resolves_and_mutes_loser(monkeypatch):
    monkeypatch.setattr(telegram_client, "get_chat_member_status", AsyncMock(return_value="member"))
    challenger_id, opponent_id = 940201, 940202
    await _ensure_user(challenger_id)
    await _ensure_user(opponent_id)
    duel_id = await _create_pending_duel(ACCEPT_CHAT_ID, challenger_id, opponent_id)

    # Форсируем challenger как победителя (accept_duel: _rng.choice([challenger_id, opponent_id])).
    monkeypatch.setattr(duel_service, "_rng", _ForcedChoiceRng(challenger_id))
    mute_mock = AsyncMock()
    monkeypatch.setattr(duel_route, "apply_mute_from_api", mute_mock)

    init_data = _build_init_data(user_id=opponent_id)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            f"/api/v1/duel/{duel_id}/accept",
            params={"chat_id": ACCEPT_CHAT_ID},
            headers={"X-Telegram-Init-Data": init_data},
            json={"ref_id": str(uuid.uuid4())},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "resolved"
    assert body["winner_id"] == challenger_id
    assert body["loser_id"] == opponent_id

    mute_mock.assert_awaited_once()
    call_args = mute_mock.await_args.args
    call_kwargs = mute_mock.await_args.kwargs
    assert opponent_id in call_args or call_kwargs.get("user_id") == opponent_id


@pytest.mark.asyncio
async def test_accept_duel_mute_failure_does_not_break_response(monkeypatch):
    """Деньги уже двинулись в duel_service.accept_duel — падение мут-пути
    не должно превратить успешный accept в ошибку (WR-05-прецедент)."""
    monkeypatch.setattr(telegram_client, "get_chat_member_status", AsyncMock(return_value="member"))
    challenger_id, opponent_id = 940203, 940204
    await _ensure_user(challenger_id)
    await _ensure_user(opponent_id)
    duel_id = await _create_pending_duel(ACCEPT_CHAT_ID, challenger_id, opponent_id)

    monkeypatch.setattr(duel_service, "_rng", _ForcedChoiceRng(challenger_id))
    monkeypatch.setattr(
        duel_route, "apply_mute_from_api", AsyncMock(side_effect=RuntimeError("telegram down"))
    )

    init_data = _build_init_data(user_id=opponent_id)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            f"/api/v1/duel/{duel_id}/accept",
            params={"chat_id": ACCEPT_CHAT_ID},
            headers={"X-Telegram-Init-Data": init_data},
            json={"ref_id": str(uuid.uuid4())},
        )

    assert resp.status_code == 200
    assert resp.json()["status"] == "resolved"


@pytest.mark.asyncio
async def test_accept_duel_by_non_invited_user_returns_400_idor(monkeypatch):
    """IDOR: только приглашённый opponent_id (из Duel-строки) может принять
    — AcceptDuelBody не несёт opponent_id вовсе, identity приходит ТОЛЬКО из
    AuthContext; сторонний пользователь не может подделать это телом
    запроса."""
    monkeypatch.setattr(telegram_client, "get_chat_member_status", AsyncMock(return_value="member"))
    challenger_id, opponent_id, stranger_id = 940205, 940206, 940207
    await _ensure_user(challenger_id)
    await _ensure_user(opponent_id)
    await _ensure_user(stranger_id)
    duel_id = await _create_pending_duel(ACCEPT_CHAT_ID, challenger_id, opponent_id)

    init_data = _build_init_data(user_id=stranger_id)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            f"/api/v1/duel/{duel_id}/accept",
            params={"chat_id": ACCEPT_CHAT_ID},
            headers={"X-Telegram-Init-Data": init_data},
            json={"ref_id": str(uuid.uuid4())},
        )

    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_accept_duel_not_found_returns_404(monkeypatch):
    monkeypatch.setattr(telegram_client, "get_chat_member_status", AsyncMock(return_value="member"))
    user_id = 940208
    await _ensure_user(user_id)
    init_data = _build_init_data(user_id=user_id)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/duel/999999999/accept",
            params={"chat_id": ACCEPT_CHAT_ID},
            headers={"X-Telegram-Init-Data": init_data},
            json={"ref_id": str(uuid.uuid4())},
        )

    assert resp.status_code == 404


# --- POST /api/v1/duel/{id}/decline + /cancel --------------------------------


@pytest.mark.asyncio
async def test_decline_duel_by_opponent_returns_declined(monkeypatch):
    monkeypatch.setattr(telegram_client, "get_chat_member_status", AsyncMock(return_value="member"))
    challenger_id, opponent_id = 940301, 940302
    await _ensure_user(challenger_id)
    await _ensure_user(opponent_id)
    duel_id = await _create_pending_duel(DECLINE_CHAT_ID, challenger_id, opponent_id)

    init_data = _build_init_data(user_id=opponent_id)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            f"/api/v1/duel/{duel_id}/decline",
            params={"chat_id": DECLINE_CHAT_ID},
            headers={"X-Telegram-Init-Data": init_data},
        )

    assert resp.status_code == 200
    assert resp.json()["status"] == "declined"


@pytest.mark.asyncio
async def test_decline_duel_by_non_opponent_returns_400_idor(monkeypatch):
    monkeypatch.setattr(telegram_client, "get_chat_member_status", AsyncMock(return_value="member"))
    challenger_id, opponent_id = 940303, 940304
    await _ensure_user(challenger_id)
    await _ensure_user(opponent_id)
    duel_id = await _create_pending_duel(DECLINE_CHAT_ID, challenger_id, opponent_id)

    # challenger пытается "отклонить" свою же дуэль — только opponent_id может.
    init_data = _build_init_data(user_id=challenger_id)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            f"/api/v1/duel/{duel_id}/decline",
            params={"chat_id": DECLINE_CHAT_ID},
            headers={"X-Telegram-Init-Data": init_data},
        )

    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_cancel_duel_by_challenger_returns_cancelled(monkeypatch):
    monkeypatch.setattr(telegram_client, "get_chat_member_status", AsyncMock(return_value="member"))
    challenger_id, opponent_id = 940401, 940402
    await _ensure_user(challenger_id)
    await _ensure_user(opponent_id)
    duel_id = await _create_pending_duel(CANCEL_CHAT_ID, challenger_id, opponent_id)

    init_data = _build_init_data(user_id=challenger_id)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            f"/api/v1/duel/{duel_id}/cancel",
            params={"chat_id": CANCEL_CHAT_ID},
            headers={"X-Telegram-Init-Data": init_data},
        )

    assert resp.status_code == 200
    assert resp.json()["status"] == "cancelled"


@pytest.mark.asyncio
async def test_cancel_duel_by_non_challenger_returns_400_idor(monkeypatch):
    monkeypatch.setattr(telegram_client, "get_chat_member_status", AsyncMock(return_value="member"))
    challenger_id, opponent_id = 940403, 940404
    await _ensure_user(challenger_id)
    await _ensure_user(opponent_id)
    duel_id = await _create_pending_duel(CANCEL_CHAT_ID, challenger_id, opponent_id)

    init_data = _build_init_data(user_id=opponent_id)  # оппонент, не челленджер

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            f"/api/v1/duel/{duel_id}/cancel",
            params={"chat_id": CANCEL_CHAT_ID},
            headers={"X-Telegram-Init-Data": init_data},
        )

    assert resp.status_code == 400


# --- POST /api/v1/duelbot -----------------------------------------------------


@pytest.mark.asyncio
async def test_duelbot_valid_returns_200_resolved(monkeypatch):
    monkeypatch.setattr(telegram_client, "get_chat_member_status", AsyncMock(return_value="member"))
    monkeypatch.setattr(duel_service, "_rng", _ForcedChoiceRng(True))
    user_id = 940501
    await _ensure_user(user_id)
    init_data = _build_init_data(user_id=user_id)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/duelbot",
            params={"chat_id": DUELBOT_CHAT_ID},
            headers={"X-Telegram-Init-Data": init_data},
            json={"stake": 20, "ref_id": str(uuid.uuid4())},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "resolved"
    assert body["winner_id"] == user_id


@pytest.mark.asyncio
async def test_duelbot_missing_init_data_returns_401():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/duelbot",
            params={"chat_id": DUELBOT_CHAT_ID},
            json={"stake": 20, "ref_id": str(uuid.uuid4())},
        )

    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_duelbot_ignores_foreign_user_id_in_body_idor(monkeypatch):
    """T-04.2-02: та же IDOR-защита, что и у coinflip/dice/roulette
    (test_api_games.py) — поддельный challenger_id "жертвы" в теле запроса
    не должен затронуть её баланс/результат, только атакующего из initData."""
    monkeypatch.setattr(telegram_client, "get_chat_member_status", AsyncMock(return_value="member"))
    monkeypatch.setattr(duel_service, "_rng", _ForcedChoiceRng(False))
    attacker_id, victim_id = 940502, 940503
    await _ensure_user(attacker_id)
    await _ensure_user(victim_id)
    init_data = _build_init_data(user_id=attacker_id)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/duelbot",
            params={"chat_id": DUELBOT_CHAT_ID},
            headers={"X-Telegram-Init-Data": init_data},
            json={"stake": 20, "ref_id": str(uuid.uuid4()), "challenger_id": victim_id},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["loser_id"] == attacker_id  # атакующий сам проиграл, не "жертва"
