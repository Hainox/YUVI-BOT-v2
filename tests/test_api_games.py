"""Тесты POST /api/v1/games/coinflip (Task 2, games.py) — против живого Postgres.

Тот же initData-хелпер и `_app_state`-фикстура, что и `test_api_economy.py`
(независимая от `api/deps.py` сборка initData, monkeypatch членства).
Денежная проверка идёт напрямую через `economy_service`/`SessionLocal()` —
тот же engine, что использует сам роут (не `session`-фикстура conftest.py).

IDOR-тест (T-04.2-02): в теле запроса намеренно НЕТ поля user_id в
Pydantic-модели роута — но атакующий может ПОПЫТАТЬСЯ протащить чужой
user_id как лишнее JSON-поле; тест доказывает, что баланс двигается только
у пользователя из initData, а не у "жертвы" из тела запроса.

--- POST /api/v1/games/slots и /games/blackjack (Task 1/2, 04.2-10) ---------

Слоты — стейтлес (как coinflip/dice/roulette), тесты той же формы.
Блэкджек — стейтфул (game_id из start-ответа переиспользуется в /action).
Детерминированная колода форсируется тем же `_FixedDeckRng`-стабом, что и
`tests/test_blackjack_service.py` (локальная копия — та же причина, что и
`_ForcedRollRng` выше: тесты этого файла не импортируют друг у друга
приватные тестовые классы между модулями).

Действие на ЧУЖОЙ game_id (`blackjack_action`) возвращает `CasinoError`
("раздача не найдена") — не `GameNotActive`: SELECT в `blackjack_action`
фильтрует ПО `user_id`, так что раздача другого игрока структурно
неотличима от несуществующей (IDOR закрыт структурно, не через отдельную
403-ветку). Действие на УЖЕ SETTLED раздаче — НЕ ошибка: `blackjack_action`
использует статус-переход "active"->"settled" как гард идемпотентности
(T-04.1-09, `04.1-03-SUMMARY.md`) и возвращает сохранённый исход 200-м
ответом (повторный no-op), а не 409 — это уже протестированное и
задокументированное поведение сервиса, роут его не переопределяет.
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
from sqlalchemy import update
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

# Отдельные chat_id для слотов/блэкджека (Task 1/2, 04.2-10) — та же изоляция
# банка/баланса, что у остальных игр этого файла, новый диапазон.
SLOTS_CHAT_ID = -900307
BLACKJACK_CHAT_ID = -900308


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


class _FixedDeckRng:
    """Локальная копия `tests/test_blackjack_service.py::_FixedDeckRng` —
    `shuffle(deck)` переставляет колоду так, чтобы `deck.pop()` (берёт с
    КОНЦА) отдавал карты строго в порядке `pop_sequence`."""

    def __init__(self, pop_sequence: list[str]):
        self._pop_sequence = pop_sequence

    def shuffle(self, deck: list[str]) -> None:
        remaining = list(deck)
        for card in self._pop_sequence:
            remaining.remove(card)
        deck[:] = remaining + list(reversed(self._pop_sequence))


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


async def _topup(chat_id: int, user_id: int, min_balance: int = 1000) -> None:
    """Тот же Rule-1 фикс, что и `tests/test_api_markets.py::_fund` (04.2-10,
    см. deferred-items.md) — фиксированные `user_id`-литералы этого файла
    против ДОЛГОЖИВУЩЕГО docker-compose Postgres-контейнера истощаются
    накопительными списаниями по мере повторных прогонов ПОЛНОГО набора
    тестов (особенно блэкджек-раздачи с детерминированным `double`, где
    ставка списывается ДВАЖДЫ на заведомо проигрышной раздаче). Топит
    баланс минимум до `min_balance` через `economy_service.credit` со
    СВЕЖИМ `ref_id` (никогда не гасится идемпотентностью)."""
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


# --- POST /api/v1/games/slots (Task 1/2, 04.2-10) -----------------------------


@pytest.mark.asyncio
async def test_slots_valid_bet_returns_200_with_settled_result(monkeypatch):
    monkeypatch.setattr(telegram_client, "get_chat_member_status", AsyncMock(return_value="member"))
    user_id = 300401
    await _ensure_user(user_id)
    await _topup(SLOTS_CHAT_ID, user_id)
    init_data = _build_init_data(user_id=user_id)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/games/slots",
            params={"chat_id": SLOTS_CHAT_ID},
            headers={"X-Telegram-Init-Data": init_data},
            json={"bet": 100, "idem_key": str(uuid.uuid4())},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["game"] == "slots"
    assert body["bet"] == 100
    assert "payout" in body
    assert len(body["outcome"]["grid"]) == 3  # 3 строки
    assert all(len(row) == 5 for row in body["outcome"]["grid"])  # 5 столбцов
    assert "wins" in body["outcome"]
    assert "freespins" in body["outcome"]
    assert "scatter" in body["outcome"]


@pytest.mark.asyncio
async def test_slots_missing_init_data_returns_401():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/games/slots",
            params={"chat_id": SLOTS_CHAT_ID},
            json={"bet": 100, "idem_key": str(uuid.uuid4())},
        )

    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_slots_bet_below_minimum_returns_400(monkeypatch):
    """bet=1 нарушает ОБА ограничения play_slots: ниже casino_min_bet И не
    кратно slot_engine.TOTAL_LINES (10) — оба пути ведут к InvalidBet->400."""
    monkeypatch.setattr(telegram_client, "get_chat_member_status", AsyncMock(return_value="member"))
    user_id = 300402
    await _ensure_user(user_id)
    await _topup(SLOTS_CHAT_ID, user_id)
    init_data = _build_init_data(user_id=user_id)
    assert 1 < settings.casino_min_bet

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/games/slots",
            params={"chat_id": SLOTS_CHAT_ID},
            headers={"X-Telegram-Init-Data": init_data},
            json={"bet": 1, "idem_key": str(uuid.uuid4())},
        )

    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_slots_bet_not_multiple_of_lines_returns_400(monkeypatch):
    monkeypatch.setattr(telegram_client, "get_chat_member_status", AsyncMock(return_value="member"))
    user_id = 300403
    await _ensure_user(user_id)
    await _topup(SLOTS_CHAT_ID, user_id)
    init_data = _build_init_data(user_id=user_id)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/games/slots",
            params={"chat_id": SLOTS_CHAT_ID},
            headers={"X-Telegram-Init-Data": init_data},
            # 25 >= casino_min_bet, но не кратно 10 (TOTAL_LINES)
            json={"bet": 25, "idem_key": str(uuid.uuid4())},
        )

    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_slots_ignores_foreign_user_id_in_body_idor(monkeypatch):
    """T-04.2-02: та же IDOR-защита, что и у coinflip/dice/roulette выше."""
    monkeypatch.setattr(telegram_client, "get_chat_member_status", AsyncMock(return_value="member"))
    attacker_id = 300404
    victim_id = 300405
    await _ensure_user(attacker_id)
    await _topup(SLOTS_CHAT_ID, attacker_id)
    await _ensure_user(victim_id)
    await _topup(SLOTS_CHAT_ID, victim_id)
    init_data = _build_init_data(user_id=attacker_id)
    idem_key = str(uuid.uuid4())

    victim_before = await _get_balance(SLOTS_CHAT_ID, victim_id)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/games/slots",
            params={"chat_id": SLOTS_CHAT_ID},
            headers={"X-Telegram-Init-Data": init_data},
            json={
                "bet": 100,
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

    victim_after = await _get_balance(SLOTS_CHAT_ID, victim_id)
    assert victim_after == victim_before


# --- POST /api/v1/games/blackjack (start) + /blackjack/{id}/action -----------
# (Task 1/2, 04.2-10) ---------------------------------------------------------


@pytest.mark.asyncio
async def test_blackjack_start_valid_bet_returns_200_with_active_hand(monkeypatch):
    monkeypatch.setattr(telegram_client, "get_chat_member_status", AsyncMock(return_value="member"))
    # 8+4 = 12 (не натурал) — раздача остаётся "active", удобно проверить
    # ровно форму start-ответа без немедленного settle.
    monkeypatch.setattr(casino_service, "_rng", _FixedDeckRng(["8", "4", "7", "5"]))
    user_id = 300501
    await _ensure_user(user_id)
    await _topup(BLACKJACK_CHAT_ID, user_id)
    init_data = _build_init_data(user_id=user_id)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/games/blackjack",
            params={"chat_id": BLACKJACK_CHAT_ID},
            headers={"X-Telegram-Init-Data": init_data},
            json={"bet": 100, "idem_key": str(uuid.uuid4())},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "active"
    assert body["bet"] == 100
    assert body["player"] == ["8", "4"]
    assert "dealer_upcard" in body
    assert "id" in body

    await _force_settle_leftover_game(body["id"])


@pytest.mark.asyncio
async def test_blackjack_start_missing_init_data_returns_401():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/games/blackjack",
            params={"chat_id": BLACKJACK_CHAT_ID},
            json={"bet": 100, "idem_key": str(uuid.uuid4())},
        )

    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_blackjack_start_bet_below_minimum_returns_400(monkeypatch):
    monkeypatch.setattr(telegram_client, "get_chat_member_status", AsyncMock(return_value="member"))
    user_id = 300502
    await _ensure_user(user_id)
    await _topup(BLACKJACK_CHAT_ID, user_id)
    init_data = _build_init_data(user_id=user_id)
    assert 1 < settings.casino_min_bet

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/games/blackjack",
            params={"chat_id": BLACKJACK_CHAT_ID},
            headers={"X-Telegram-Init-Data": init_data},
            json={"bet": 1, "idem_key": str(uuid.uuid4())},
        )

    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_blackjack_start_ignores_foreign_user_id_in_body_idor(monkeypatch):
    """T-04.2-02: та же IDOR-защита, что и у остальных игр этого файла."""
    monkeypatch.setattr(telegram_client, "get_chat_member_status", AsyncMock(return_value="member"))
    attacker_id = 300503
    victim_id = 300504
    await _ensure_user(attacker_id)
    await _topup(BLACKJACK_CHAT_ID, attacker_id)
    await _ensure_user(victim_id)
    await _topup(BLACKJACK_CHAT_ID, victim_id)
    init_data = _build_init_data(user_id=attacker_id)
    idem_key = str(uuid.uuid4())

    victim_before = await _get_balance(BLACKJACK_CHAT_ID, victim_id)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/games/blackjack",
            params={"chat_id": BLACKJACK_CHAT_ID},
            headers={"X-Telegram-Init-Data": init_data},
            json={
                "bet": 100,
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

    victim_after = await _get_balance(BLACKJACK_CHAT_ID, victim_id)
    assert victim_after == victim_before

    # Реальный RNG (без forced-колоды) — раздача обычно остаётся "active"
    # (натурал редок, ~4.8%); cleanup безопасен как no-op, если уже settled.
    await _force_settle_leftover_game(game_row.id)


async def _start_fixed_hand(client, init_data: str, chat_id: int, pop_sequence: list[str]) -> int:
    """Хелпер: раздаёт детерминированную (не-натурал) раздачу через реальный
    HTTP start-роут, возвращает `game_id` из ответа. `casino_service._rng`
    ДОЛЖЕН быть замонкипатчен `_FixedDeckRng(pop_sequence)` ДО вызова."""
    resp = await client.post(
        "/api/v1/games/blackjack",
        params={"chat_id": chat_id},
        headers={"X-Telegram-Init-Data": init_data},
        json={"bet": 100, "idem_key": str(uuid.uuid4())},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "active"
    return body["id"]


async def _force_settle_leftover_game(game_id: int) -> None:
    """Cleanup для тестов, которые НАМЕРЕННО оставляют раздачу блэкджека в
    status="active" после своих assert'ов (проверяют форму именно активного
    ответа). `casino_service.resolve_blackjack_timeouts` сканирует ВСЕ
    активные раздачи ГЛОБАЛЬНО (не по chat_id) с истёкшим `turn_deadline` —
    без этой уборки такая раздача осталась бы висеть с 60с (D-07) дедлайном
    и, если полный прогон suite'а займёт больше минуты, попала бы в батч
    `tests/test_blackjack_service.py`'s таймаут-тестов, раздувая их
    `resolved_count` (тот же класс cross-test-полюции, что уже
    задокументирован для `test_place_bet_closed_market_returns_409` в
    `test_api_markets.py` — прямая ORM-правка статуса, без побочных
    денежных эффектов)."""
    async with SessionLocal() as cleanup_session:
        await cleanup_session.execute(
            update(CasinoGame).where(CasinoGame.id == game_id).values(status="settled")
        )
        await cleanup_session.commit()


@pytest.mark.asyncio
async def test_blackjack_action_hit_steps_hand_and_stays_active(monkeypatch):
    monkeypatch.setattr(telegram_client, "get_chat_member_status", AsyncMock(return_value="member"))
    user_id = 300505
    await _ensure_user(user_id)
    await _topup(BLACKJACK_CHAT_ID, user_id)
    init_data = _build_init_data(user_id=user_id)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # player=[8,4]=12, dealer=[7,5]=12; hit добирает "3" -> player=[8,4,3]=15 (не bust)
        monkeypatch.setattr(casino_service, "_rng", _FixedDeckRng(["8", "4", "7", "5", "3"]))
        game_id = await _start_fixed_hand(client, init_data, BLACKJACK_CHAT_ID, [])

        resp = await client.post(
            f"/api/v1/games/blackjack/{game_id}/action",
            params={"chat_id": BLACKJACK_CHAT_ID},
            headers={"X-Telegram-Init-Data": init_data},
            json={"action": "hit"},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "active"
    assert body["player"] == ["8", "4", "3"]

    await _force_settle_leftover_game(game_id)


@pytest.mark.asyncio
async def test_blackjack_action_stand_settles_hand(monkeypatch):
    monkeypatch.setattr(telegram_client, "get_chat_member_status", AsyncMock(return_value="member"))
    user_id = 300506
    await _ensure_user(user_id)
    await _topup(BLACKJACK_CHAT_ID, user_id)
    init_data = _build_init_data(user_id=user_id)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # player=[8,4]=12, dealer=[7,5]=12 -> stand доигрывает дилера: +"9" -> 21, дилер стоп.
        # player(12) < dealer(21) -> "lose".
        monkeypatch.setattr(casino_service, "_rng", _FixedDeckRng(["8", "4", "7", "5", "9"]))
        game_id = await _start_fixed_hand(client, init_data, BLACKJACK_CHAT_ID, [])

        resp = await client.post(
            f"/api/v1/games/blackjack/{game_id}/action",
            params={"chat_id": BLACKJACK_CHAT_ID},
            headers={"X-Telegram-Init-Data": init_data},
            json={"action": "stand"},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "settled"
    assert body["dealer"] == ["7", "5", "9"]
    assert body["outcome"]["result"] == "lose"
    assert body["payout"] == 0


@pytest.mark.asyncio
async def test_blackjack_action_double_debits_second_stake_and_settles(monkeypatch):
    monkeypatch.setattr(telegram_client, "get_chat_member_status", AsyncMock(return_value="member"))
    user_id = 300507
    await _ensure_user(user_id)
    await _topup(BLACKJACK_CHAT_ID, user_id)
    init_data = _build_init_data(user_id=user_id)
    balance_before_double = None

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # player=[8,4]=12, dealer=[7,5]=12; double добирает РОВНО одну карту
        # "2" -> player=[8,4,2]=14 (не bust), затем дилер доигрывает "6" -> 18.
        # player(14) < dealer(18) -> "lose", ставка была удвоена (списана дважды).
        monkeypatch.setattr(
            casino_service, "_rng", _FixedDeckRng(["8", "4", "7", "5", "2", "6"])
        )
        game_id = await _start_fixed_hand(client, init_data, BLACKJACK_CHAT_ID, [])
        balance_before_double = await _get_balance(BLACKJACK_CHAT_ID, user_id)

        resp = await client.post(
            f"/api/v1/games/blackjack/{game_id}/action",
            params={"chat_id": BLACKJACK_CHAT_ID},
            headers={"X-Telegram-Init-Data": init_data},
            json={"action": "double"},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "settled"
    assert body["player"] == ["8", "4", "2"]
    assert body["dealer"] == ["7", "5", "6"]
    assert body["outcome"]["result"] == "lose"
    assert body["payout"] == 0

    balance_after_double = await _get_balance(BLACKJACK_CHAT_ID, user_id)
    # Проигрыш (payout=0) после удвоения списывает ВТОРУЮ ставку (100) сверх
    # уже списанной стартовой — баланс падает ещё на 100.
    assert balance_after_double == balance_before_double - 100


@pytest.mark.asyncio
async def test_blackjack_action_double_after_hit_returns_400(monkeypatch):
    """double требует РОВНО двухкарточную раздачу — после hit (3 карты)
    попытка double должна упасть InvalidBet->400."""
    monkeypatch.setattr(telegram_client, "get_chat_member_status", AsyncMock(return_value="member"))
    user_id = 300508
    await _ensure_user(user_id)
    await _topup(BLACKJACK_CHAT_ID, user_id)
    init_data = _build_init_data(user_id=user_id)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        monkeypatch.setattr(casino_service, "_rng", _FixedDeckRng(["8", "4", "7", "5", "3"]))
        game_id = await _start_fixed_hand(client, init_data, BLACKJACK_CHAT_ID, [])

        hit_resp = await client.post(
            f"/api/v1/games/blackjack/{game_id}/action",
            params={"chat_id": BLACKJACK_CHAT_ID},
            headers={"X-Telegram-Init-Data": init_data},
            json={"action": "hit"},
        )
        assert hit_resp.status_code == 200
        assert hit_resp.json()["status"] == "active"

        double_resp = await client.post(
            f"/api/v1/games/blackjack/{game_id}/action",
            params={"chat_id": BLACKJACK_CHAT_ID},
            headers={"X-Telegram-Init-Data": init_data},
            json={"action": "double"},
        )

    assert double_resp.status_code == 400

    await _force_settle_leftover_game(game_id)


@pytest.mark.asyncio
async def test_blackjack_action_invalid_action_value_returns_400(monkeypatch):
    monkeypatch.setattr(telegram_client, "get_chat_member_status", AsyncMock(return_value="member"))
    user_id = 300509
    await _ensure_user(user_id)
    await _topup(BLACKJACK_CHAT_ID, user_id)
    init_data = _build_init_data(user_id=user_id)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        monkeypatch.setattr(casino_service, "_rng", _FixedDeckRng(["8", "4", "7", "5"]))
        game_id = await _start_fixed_hand(client, init_data, BLACKJACK_CHAT_ID, [])

        resp = await client.post(
            f"/api/v1/games/blackjack/{game_id}/action",
            params={"chat_id": BLACKJACK_CHAT_ID},
            headers={"X-Telegram-Init-Data": init_data},
            json={"action": "surrender"},
        )

    assert resp.status_code == 400

    await _force_settle_leftover_game(game_id)


@pytest.mark.asyncio
async def test_blackjack_action_missing_init_data_returns_401():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/games/blackjack/999999/action",
            params={"chat_id": BLACKJACK_CHAT_ID},
            json={"action": "stand"},
        )

    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_blackjack_action_on_foreign_game_returns_404_idor(monkeypatch):
    """T-04.2-02 (блэкджек): чужой user_id в body невозможен вовсе (его нет
    в Pydantic-модели), но НАСТОЯЩАЯ IDOR-проверка здесь — attacker не может
    подействовать на game_id ЖЕРТВЫ, используя СВОЙ initData. SELECT в
    `blackjack_action` фильтрует по user_id из AuthContext -> чужая раздача
    структурно неотличима от несуществующей -> CasinoError -> 404. Раздача
    жертвы и её баланс должны остаться нетронутыми."""
    monkeypatch.setattr(telegram_client, "get_chat_member_status", AsyncMock(return_value="member"))
    attacker_id = 300510
    victim_id = 300511
    await _ensure_user(attacker_id)
    await _topup(BLACKJACK_CHAT_ID, attacker_id)
    await _ensure_user(victim_id)
    await _topup(BLACKJACK_CHAT_ID, victim_id)
    attacker_init_data = _build_init_data(user_id=attacker_id)
    victim_init_data = _build_init_data(user_id=victim_id)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        monkeypatch.setattr(casino_service, "_rng", _FixedDeckRng(["8", "4", "7", "5"]))
        victim_game_id = await _start_fixed_hand(
            client, victim_init_data, BLACKJACK_CHAT_ID, []
        )
        victim_balance_before = await _get_balance(BLACKJACK_CHAT_ID, victim_id)

        resp = await client.post(
            f"/api/v1/games/blackjack/{victim_game_id}/action",
            params={"chat_id": BLACKJACK_CHAT_ID},
            headers={"X-Telegram-Init-Data": attacker_init_data},
            json={"action": "stand"},
        )

    assert resp.status_code == 404

    async with SessionLocal() as verify_session:
        victim_game = (
            await verify_session.execute(
                select(CasinoGame).where(CasinoGame.id == victim_game_id)
            )
        ).scalar_one()
    assert victim_game.status == "active"  # жертва не задета вовсе
    assert victim_game.user_id == victim_id

    victim_balance_after = await _get_balance(BLACKJACK_CHAT_ID, victim_id)
    assert victim_balance_after == victim_balance_before

    await _force_settle_leftover_game(victim_game_id)


@pytest.mark.asyncio
async def test_blackjack_action_on_settled_game_replays_stored_result(monkeypatch):
    """T-04.1-09 (уже протестировано/задокументировано в 04.1-03): действие
    на уже settled раздаче — идемпотентный no-op, роут возвращает 200 с
    сохранённым исходом, а не ошибку. Деньги не двигаются повторно."""
    monkeypatch.setattr(telegram_client, "get_chat_member_status", AsyncMock(return_value="member"))
    user_id = 300512
    await _ensure_user(user_id)
    await _topup(BLACKJACK_CHAT_ID, user_id)
    init_data = _build_init_data(user_id=user_id)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        monkeypatch.setattr(casino_service, "_rng", _FixedDeckRng(["8", "4", "7", "5", "9"]))
        game_id = await _start_fixed_hand(client, init_data, BLACKJACK_CHAT_ID, [])

        first = await client.post(
            f"/api/v1/games/blackjack/{game_id}/action",
            params={"chat_id": BLACKJACK_CHAT_ID},
            headers={"X-Telegram-Init-Data": init_data},
            json={"action": "stand"},
        )
        assert first.status_code == 200
        assert first.json()["status"] == "settled"
        balance_after_settle = await _get_balance(BLACKJACK_CHAT_ID, user_id)

        second = await client.post(
            f"/api/v1/games/blackjack/{game_id}/action",
            params={"chat_id": BLACKJACK_CHAT_ID},
            headers={"X-Telegram-Init-Data": init_data},
            json={"action": "stand"},
        )

    assert second.status_code == 200
    body = second.json()
    assert body["status"] == "settled"
    assert body["outcome"] == first.json()["outcome"]
    assert body["payout"] == first.json()["payout"]

    balance_after_replay = await _get_balance(BLACKJACK_CHAT_ID, user_id)
    assert balance_after_replay == balance_after_settle  # деньги не двинулись повторно
