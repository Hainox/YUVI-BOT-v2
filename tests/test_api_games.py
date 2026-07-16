"""Тесты POST /api/v1/games/coinflip (Task 2, games.py) — против живого Postgres.

Тот же initData-хелпер и `_app_state`-фикстура, что и `test_api_economy.py`
(независимая от `api/deps.py` сборка initData, monkeypatch членства).
Денежная проверка идёт напрямую через `economy_service`/`SessionLocal()` —
тот же engine, что использует сам роут (не `session`-фикстура conftest.py).

IDOR-тест (T-04.2-02): в теле запроса намеренно НЕТ поля user_id в
Pydantic-модели роута — но атакующий может ПОПЫТАТЬСЯ протащить чужой
user_id как лишнее JSON-поле; тест доказывает, что баланс двигается только
у пользователя из initData, а не у "жертвы" из тела запроса.
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
from bot.services import casino_service
from bot.services import economy_service
from common.db.session import engine
from common.db.session import SessionLocal
from common.models.casino_game import CasinoGame
from common.models.user import User

CHAT_ID = -900301
# Отдельный chat_id ТОЛЬКО для bank_capped-теста ниже — гарантирует чистый
# нулевой chat_bank (не смешивается с балансом банка, накопленным другими
# тестами этого файла в CHAT_ID).
FRESH_BANK_CHAT_ID = -900302

# Отдельные chat_id для кости/рулетки (Task 1/2, 04.2-03) — та же изоляция
# банка, что у coinflip выше (CHAT_ID/FRESH_BANK_CHAT_ID), просто новый
# диапазон, чтобы не смешивать баланс/банк с coinflip-тестами этого файла.
DICE_CHAT_ID = -900303
DICE_FRESH_BANK_CHAT_ID = -900304
ROULETTE_CHAT_ID = -900305
ROULETTE_FRESH_BANK_CHAT_ID = -900306


class _ForcedWinRng:
    """Форсирует детерминированный выигрыш coinflip (см. test_casino_service.py::
    _ForcedRng) — `_rng.choice(["heads", "tails"])` внутри `play_coinflip.compute()`
    всегда возвращает то же значение, что и `choice` в теле запроса."""

    def __init__(self, forced_result: str):
        self._forced_result = forced_result

    def choice(self, seq):
        return self._forced_result


class _ForcedRollRng:
    """Форсирует детерминированный `_rng.randint(a, b)` внутри `play_dice`/
    `play_roulette.compute()` (см. test_casino_service.py::_ForcedRng,
    вариант, специализированный только под randint — choice() здесь не
    нужен)."""

    def __init__(self, forced_value: int):
        self._forced_value = forced_value

    def randint(self, a: int, b: int) -> int:
        return self._forced_value


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


@pytest.fixture(autouse=True)
def _reset_membership_cache():
    telegram_client.reset_cache()
    yield
    telegram_client.reset_cache()


@pytest.fixture(autouse=True)
async def _fresh_engine_per_test():
    """См. `test_api_economy.py::_fresh_engine_per_test` — тот же
    процесс-глобальный engine, та же необходимость `dispose()` между тестами
    с разными event loop'ами (pytest-asyncio, function-scoped loop)."""
    await engine.dispose()
    yield
    await engine.dispose()


@pytest.fixture(autouse=True)
def _app_state():
    app.state.http_client = AsyncMock()
    app.state.redis = None
    yield


@pytest.mark.asyncio
async def test_coinflip_valid_bet_returns_200_with_settled_result(monkeypatch):
    monkeypatch.setattr(telegram_client, "get_chat_member_status", AsyncMock(return_value="member"))
    user_id = 300101
    await _ensure_user(user_id)
    init_data = _build_init_data(user_id=user_id)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/games/coinflip",
            params={"chat_id": CHAT_ID},
            headers={"X-Telegram-Init-Data": init_data},
            json={"bet": 20, "choice": "heads", "idem_key": str(uuid.uuid4())},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["game"] == "coinflip"
    assert body["bet"] == 20
    assert "payout" in body
    assert "outcome" in body


@pytest.mark.asyncio
async def test_coinflip_missing_init_data_returns_401():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/games/coinflip",
            params={"chat_id": CHAT_ID},
            json={"bet": 20, "choice": "heads", "idem_key": str(uuid.uuid4())},
        )

    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_coinflip_forged_init_data_returns_401(monkeypatch):
    monkeypatch.setattr(telegram_client, "get_chat_member_status", AsyncMock(return_value="member"))
    init_data = _build_init_data(user_id=300102, tamper=True)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/games/coinflip",
            params={"chat_id": CHAT_ID},
            headers={"X-Telegram-Init-Data": init_data},
            json={"bet": 20, "choice": "heads", "idem_key": str(uuid.uuid4())},
        )

    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_coinflip_bet_below_minimum_returns_400(monkeypatch):
    monkeypatch.setattr(telegram_client, "get_chat_member_status", AsyncMock(return_value="member"))
    user_id = 300103
    await _ensure_user(user_id)
    init_data = _build_init_data(user_id=user_id)
    assert 1 < settings.casino_min_bet  # гарантирует, что bet=1 реально ниже минимума

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/games/coinflip",
            params={"chat_id": CHAT_ID},
            headers={"X-Telegram-Init-Data": init_data},
            json={"bet": 1, "choice": "heads", "idem_key": str(uuid.uuid4())},
        )

    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_coinflip_ignores_foreign_user_id_in_body_idor(monkeypatch):
    """T-04.2-02: user_id/chat_id берутся ТОЛЬКО из AuthContext — поддельный
    user_id "жертвы" в теле запроса не должен сдвинуть ЕЁ баланс, только
    баланс реального пользователя из initData (атакующего)."""
    monkeypatch.setattr(telegram_client, "get_chat_member_status", AsyncMock(return_value="member"))
    attacker_id = 300104
    victim_id = 300105
    await _ensure_user(attacker_id)
    await _ensure_user(victim_id)
    init_data = _build_init_data(user_id=attacker_id)
    idem_key = str(uuid.uuid4())

    victim_before = await _get_balance(CHAT_ID, victim_id)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/games/coinflip",
            params={"chat_id": CHAT_ID},
            headers={"X-Telegram-Init-Data": init_data},
            json={
                "bet": 20,
                "choice": "heads",
                "idem_key": idem_key,
                "user_id": victim_id,  # атакующий пытается подставить чужой user_id
            },
        )

    assert resp.status_code == 200

    # Прямая проверка: раунд реально записан за атакующего (initData), а не
    # за "жертву" из тела запроса — не полагаемся на дельту баланса (payout
    # может быть урезан D-06 капом банка до net-0 на выигрыше, это не баг,
    # а корректное поведение _settle/pay_from_bank).
    async with SessionLocal() as verify_session:
        game_row = (
            await verify_session.execute(
                select(CasinoGame).where(CasinoGame.idem_key == idem_key)
            )
        ).scalar_one()
    assert game_row.user_id == attacker_id

    victim_after = await _get_balance(CHAT_ID, victim_id)
    assert victim_after == victim_before  # жертва не затронута вовсе


@pytest.mark.asyncio
async def test_coinflip_win_on_empty_bank_reports_bank_capped(monkeypatch):
    """Регрессия по реальному инциденту живой Telegram-верификации 04.2-02:
    первый раунд в чате со свежим (нулевым) chat_bank выиграл по RNG, но
    D-06 (`pay_from_bank`) урезал выплату до размера самой ставки — баланс
    игрока не изменился, хотя раунд был выигран (1000 -> 1000). Без явного
    флага это выглядит для игрока как "баланс не обновился после победы".
    Роут теперь обязан вернуть `bank_capped: true` в этом случае."""
    monkeypatch.setattr(telegram_client, "get_chat_member_status", AsyncMock(return_value="member"))
    monkeypatch.setattr(casino_service, "_rng", _ForcedWinRng("heads"))
    user_id = 300106
    await _ensure_user(user_id)
    init_data = _build_init_data(user_id=user_id)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/games/coinflip",
            params={"chat_id": FRESH_BANK_CHAT_ID},
            headers={"X-Telegram-Init-Data": init_data},
            json={"bet": 20, "choice": "heads", "idem_key": str(uuid.uuid4())},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["outcome"]["won"] is True
    # Банк чата стартовал с 0, был пополнен ровно ставкой (20) самим же
    # раундом до выплаты — честный payout (20 * 1.98 = 39) не влезает,
    # capped-выплата == ставке.
    assert body["payout"] == 20
    assert body["bank_capped"] is True


# --- POST /api/v1/games/dice (Task 1/2, 04.2-03) -----------------------------


@pytest.mark.asyncio
async def test_dice_valid_bet_returns_200_with_settled_result(monkeypatch):
    monkeypatch.setattr(telegram_client, "get_chat_member_status", AsyncMock(return_value="member"))
    user_id = 300201
    await _ensure_user(user_id)
    init_data = _build_init_data(user_id=user_id)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/games/dice",
            params={"chat_id": DICE_CHAT_ID},
            headers={"X-Telegram-Init-Data": init_data},
            json={"bet": 20, "target": 50, "direction": "under", "idem_key": str(uuid.uuid4())},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["game"] == "dice"
    assert body["bet"] == 20
    assert "payout" in body
    assert "outcome" in body


@pytest.mark.asyncio
async def test_dice_missing_init_data_returns_401():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/games/dice",
            params={"chat_id": DICE_CHAT_ID},
            json={"bet": 20, "target": 50, "direction": "under", "idem_key": str(uuid.uuid4())},
        )

    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_dice_out_of_range_target_returns_400(monkeypatch):
    monkeypatch.setattr(telegram_client, "get_chat_member_status", AsyncMock(return_value="member"))
    user_id = 300202
    await _ensure_user(user_id)
    init_data = _build_init_data(user_id=user_id)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/games/dice",
            params={"chat_id": DICE_CHAT_ID},
            headers={"X-Telegram-Init-Data": init_data},
            json={"bet": 20, "target": 999, "direction": "under", "idem_key": str(uuid.uuid4())},
        )

    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_dice_bet_below_minimum_returns_400(monkeypatch):
    monkeypatch.setattr(telegram_client, "get_chat_member_status", AsyncMock(return_value="member"))
    user_id = 300203
    await _ensure_user(user_id)
    init_data = _build_init_data(user_id=user_id)
    assert 1 < settings.casino_min_bet  # гарантирует, что bet=1 реально ниже минимума

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/games/dice",
            params={"chat_id": DICE_CHAT_ID},
            headers={"X-Telegram-Init-Data": init_data},
            json={"bet": 1, "target": 50, "direction": "under", "idem_key": str(uuid.uuid4())},
        )

    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_dice_ignores_foreign_user_id_in_body_idor(monkeypatch):
    """T-04.2-02: та же IDOR-защита, что и у coinflip выше — поддельный
    user_id "жертвы" в теле запроса не должен сдвинуть её баланс."""
    monkeypatch.setattr(telegram_client, "get_chat_member_status", AsyncMock(return_value="member"))
    monkeypatch.setattr(casino_service, "_rng", _ForcedRollRng(1))  # under-win гарантирован
    attacker_id = 300204
    victim_id = 300205
    await _ensure_user(attacker_id)
    await _ensure_user(victim_id)
    init_data = _build_init_data(user_id=attacker_id)
    idem_key = str(uuid.uuid4())

    victim_before = await _get_balance(DICE_CHAT_ID, victim_id)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/games/dice",
            params={"chat_id": DICE_CHAT_ID},
            headers={"X-Telegram-Init-Data": init_data},
            json={
                "bet": 20,
                "target": 50,
                "direction": "under",
                "idem_key": idem_key,
                "user_id": victim_id,  # атакующий пытается подставить чужой user_id
            },
        )

    assert resp.status_code == 200

    async with SessionLocal() as verify_session:
        game_row = (
            await verify_session.execute(select(CasinoGame).where(CasinoGame.idem_key == idem_key))
        ).scalar_one()
    assert game_row.user_id == attacker_id

    victim_after = await _get_balance(DICE_CHAT_ID, victim_id)
    assert victim_after == victim_before


@pytest.mark.asyncio
async def test_dice_win_on_empty_bank_reports_bank_capped(monkeypatch):
    """Тот же D-06 edge-case, что у coinflip (test_coinflip_win_on_empty_bank_
    reports_bank_capped) — свежий (нулевой) chat_bank не может покрыть
    честную выплату dice, роут обязан вернуть bank_capped: true."""
    monkeypatch.setattr(telegram_client, "get_chat_member_status", AsyncMock(return_value="member"))
    monkeypatch.setattr(casino_service, "_rng", _ForcedRollRng(1))  # roll=1 < target=50 => under wins
    user_id = 300206
    await _ensure_user(user_id)
    init_data = _build_init_data(user_id=user_id)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/games/dice",
            params={"chat_id": DICE_FRESH_BANK_CHAT_ID},
            headers={"X-Telegram-Init-Data": init_data},
            json={"bet": 20, "target": 50, "direction": "under", "idem_key": str(uuid.uuid4())},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["outcome"]["won"] is True
    # Банк стартовал с 0, пополнен ровно ставкой (20) до выплаты — честный
    # payout (20 * 0.98 / 0.49 = 39) не влезает, capped-выплата == ставке.
    assert body["payout"] == 20
    assert body["bank_capped"] is True


# --- POST /api/v1/games/roulette (Task 1/2, 04.2-03) -------------------------


@pytest.mark.asyncio
async def test_roulette_valid_bet_returns_200_with_settled_result(monkeypatch):
    monkeypatch.setattr(telegram_client, "get_chat_member_status", AsyncMock(return_value="member"))
    user_id = 300301
    await _ensure_user(user_id)
    init_data = _build_init_data(user_id=user_id)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/games/roulette",
            params={"chat_id": ROULETTE_CHAT_ID},
            headers={"X-Telegram-Init-Data": init_data},
            json={"bet": 10, "bet_type": "color", "bet_value": "red", "idem_key": str(uuid.uuid4())},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["game"] == "roulette"
    assert body["bet"] == 10
    assert "payout" in body
    assert "outcome" in body


@pytest.mark.asyncio
async def test_roulette_missing_init_data_returns_401():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/games/roulette",
            params={"chat_id": ROULETTE_CHAT_ID},
            json={"bet": 10, "bet_type": "color", "bet_value": "red", "idem_key": str(uuid.uuid4())},
        )

    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_roulette_invalid_bet_value_returns_400(monkeypatch):
    monkeypatch.setattr(telegram_client, "get_chat_member_status", AsyncMock(return_value="member"))
    user_id = 300302
    await _ensure_user(user_id)
    init_data = _build_init_data(user_id=user_id)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/games/roulette",
            params={"chat_id": ROULETTE_CHAT_ID},
            headers={"X-Telegram-Init-Data": init_data},
            # 'purple' невалиден для bet_type='color' (WR-06, casino_service.
            # _validate_roulette_bet_value)
            json={"bet": 10, "bet_type": "color", "bet_value": "purple", "idem_key": str(uuid.uuid4())},
        )

    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_roulette_ignores_foreign_user_id_in_body_idor(monkeypatch):
    """T-04.2-02: та же IDOR-защита, что и у coinflip/dice выше."""
    monkeypatch.setattr(telegram_client, "get_chat_member_status", AsyncMock(return_value="member"))
    monkeypatch.setattr(casino_service, "_rng", _ForcedRollRng(1))  # spin=1 (red) — number-независимо
    attacker_id = 300303
    victim_id = 300304
    await _ensure_user(attacker_id)
    await _ensure_user(victim_id)
    init_data = _build_init_data(user_id=attacker_id)
    idem_key = str(uuid.uuid4())

    victim_before = await _get_balance(ROULETTE_CHAT_ID, victim_id)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/games/roulette",
            params={"chat_id": ROULETTE_CHAT_ID},
            headers={"X-Telegram-Init-Data": init_data},
            json={
                "bet": 10,
                "bet_type": "color",
                "bet_value": "red",
                "idem_key": idem_key,
                "user_id": victim_id,  # атакующий пытается подставить чужой user_id
            },
        )

    assert resp.status_code == 200

    async with SessionLocal() as verify_session:
        game_row = (
            await verify_session.execute(select(CasinoGame).where(CasinoGame.idem_key == idem_key))
        ).scalar_one()
    assert game_row.user_id == attacker_id

    victim_after = await _get_balance(ROULETTE_CHAT_ID, victim_id)
    assert victim_after == victim_before


@pytest.mark.asyncio
async def test_roulette_win_on_empty_bank_reports_bank_capped(monkeypatch):
    """Тот же D-06 edge-case, что у coinflip/dice — свежий (нулевой)
    chat_bank не может покрыть честную выплату рулетки (2x на color),
    роут обязан вернуть bank_capped: true."""
    monkeypatch.setattr(telegram_client, "get_chat_member_status", AsyncMock(return_value="member"))
    monkeypatch.setattr(casino_service, "_rng", _ForcedRollRng(1))  # spin=1 — красное
    user_id = 300305
    await _ensure_user(user_id)
    init_data = _build_init_data(user_id=user_id)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/games/roulette",
            params={"chat_id": ROULETTE_FRESH_BANK_CHAT_ID},
            headers={"X-Telegram-Init-Data": init_data},
            json={"bet": 20, "bet_type": "color", "bet_value": "red", "idem_key": str(uuid.uuid4())},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["outcome"]["won"] is True
    # Банк стартовал с 0, пополнен ровно ставкой (20) до выплаты — честный
    # payout (20 * 2 = 40) не влезает, capped-выплата == ставке.
    assert body["payout"] == 20
    assert body["bank_capped"] is True
