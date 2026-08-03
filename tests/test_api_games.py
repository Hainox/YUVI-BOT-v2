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
import random
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
from bot.services import slot_engine
from bot.services import teto_slot_engine
from common.db.session import engine
from common.db.session import SessionLocal
from common.models.casino_game import CasinoGame
from common.models.chat_bank import ChatBank
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
# Отдельный chat_id для GET /games/slots/jackpot (CASINO-06) — свежий пул,
# не смешивается с пулом, накопленным остальными slots-тестами в SLOTS_CHAT_ID.
JACKPOT_CHAT_ID = -900309
# Отдельный СВЕЖИЙ chat_id (bank_balance стартует с 0) для регрессии
# "джекпот + пустой банк -> +0¥ анонс не должен уйти" — не смешивается с
# банком/пулом, уже накопленными JACKPOT_CHAT_ID остальными тестами выше.
JACKPOT_EMPTY_BANK_CHAT_ID = -900310

# Отдельный chat_id для POST /games/teto_slots (04.1-XX money-интеграция) —
# НЕ переиспользует SLOTS_CHAT_ID: троттлинг-дикт `_last_slots_spin_at`
# общий для Azumanga/Тето по user_id (не по chat_id), но отдельный chat_id
# всё равно нужен, чтобы баланс/банк Тето-тестов не смешивался с уже
# накопленными в SLOTS_CHAT_ID остальными slots-тестами выше (те же
# соображения изоляции, что и у остальных диапазонов этого файла); заодно
# используются отдельные user_id (см. ниже), чтобы не делить троттлинг-
# состояние с конкретными slots-тестами.
TETO_SLOTS_CHAT_ID = -900311
# Отдельный chat_id ТОЛЬКО под регрессию "выигрыш Тето на выеденном банке"
# (bank_capped) — та же изоляция, что FRESH_BANK_CHAT_ID у coinflip: банк
# этого чата тест обнуляет явно перед спином и не делит ни с одним другим
# тестом файла (иначе накопленные ставки соседей случайно покрыли бы выигрыш
# и кап просто не наступил бы).
TETO_DRAINED_BANK_CHAT_ID = -900312


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


class _ForcedJackpotGridRng:
    """Форсирует ОДНОВРЕМЕННО сетку слота (без выигрышных линий/скаттера —
    payout=0, чтобы не путать обычный выигрыш с джекпотом в ассертах) И
    джекпот-кубик (CASINO-06) — та же пара RNG-вызовов, между которыми
    `casino_service.play_slots` делит общий `_rng`: `choice()` внутри
    `slot_engine.spin_grid`, `randint()` внутри
    `jackpot_service.contribute_and_maybe_award`. `jackpot_randint=1` форсирует
    выигрыш джекпота, любое другое значение — проигрыш (никогда не 1)."""

    def __init__(self, choices_sequence, jackpot_randint: int = 1):
        self._seq = list(choices_sequence)
        self._i = 0
        self._jackpot_randint = jackpot_randint

    def choice(self, seq):
        value = self._seq[self._i % len(self._seq)]
        self._i += 1
        return value

    def randint(self, a: int, b: int) -> int:
        return self._jackpot_randint


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
    await _topup(DICE_CHAT_ID, user_id)
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
    # CASINO-06: свежий (не replay) спин всегда несёт джекпот-слой.
    assert body["jackpot"] is not None
    assert "won" in body["jackpot"]
    assert "pool" in body["jackpot"]


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
async def test_slots_rapid_second_spin_returns_429(monkeypatch):
    """Анти-абьюз авто-спина (casino_service._check_slots_throttle): два спина
    одного игрока без реальной паузы между ними — второй получает 429, а не
    случайно проходит из-за скорости тестового раннера."""
    monkeypatch.setattr(telegram_client, "get_chat_member_status", AsyncMock(return_value="member"))
    monkeypatch.setattr(casino_service, "_last_slots_spin_at", {})
    user_id = 300410
    await _ensure_user(user_id)
    await _topup(SLOTS_CHAT_ID, user_id)
    init_data = _build_init_data(user_id=user_id)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        first = await client.post(
            "/api/v1/games/slots",
            params={"chat_id": SLOTS_CHAT_ID},
            headers={"X-Telegram-Init-Data": init_data},
            json={"bet": 100, "idem_key": str(uuid.uuid4())},
        )
        second = await client.post(
            "/api/v1/games/slots",
            params={"chat_id": SLOTS_CHAT_ID},
            headers={"X-Telegram-Init-Data": init_data},
            json={"bet": 100, "idem_key": str(uuid.uuid4())},
        )

    assert first.status_code == 200
    assert second.status_code == 429


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


# --- POST /api/v1/games/teto_slots ("Тето Брейнрот: Дрель-Хант") -------------
#
# Money-интеграция (`casino_service.play_teto_slots`) уже полностью
# протестирована в `tests/test_casino_service.py`; здесь — только тонкий
# API-слой, тот же набор сценариев, что и у POST /games/slots выше, минус
# джекпот (которого у Тето нет вовсе — `play_teto_slots` не возвращает ключ
# "jackpot") и минус bank_capped (см. докстринг api/routes/games.py).


@pytest.mark.asyncio
async def test_teto_slots_valid_bet_returns_200_with_settled_result(monkeypatch):
    monkeypatch.setattr(telegram_client, "get_chat_member_status", AsyncMock(return_value="member"))
    user_id = 300420
    await _ensure_user(user_id)
    await _topup(TETO_SLOTS_CHAT_ID, user_id)
    init_data = _build_init_data(user_id=user_id)
    bet = 10 * teto_slot_engine.TOTAL_LINES

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/games/teto_slots",
            params={"chat_id": TETO_SLOTS_CHAT_ID},
            headers={"X-Telegram-Init-Data": init_data},
            json={"bet": bet, "idem_key": str(uuid.uuid4())},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["game"] == "teto_slots"
    assert body["bet"] == bet
    assert "payout" in body
    # Реальный RNG, без форса — структурная проверка формы (см.
    # teto_slot_engine.play_one_spin/serialize_spin_result докстринги), не
    # конкретного исхода, тот же подход, что и test_slots_valid_bet_ выше.
    outcome = body["outcome"]
    assert "scatter_count" in outcome
    assert "freespins_awarded" in outcome
    assert "freespins_played" in outcome
    assert "final_blocks" in outcome
    # Azumanga-специфика (CASINO-06 джекпот) намеренно отсутствует у Тето:
    # play_teto_slots не возвращает ключ "jackpot" вовсе (см. её докстринг в
    # casino_service.py) — доказываем, что роут не унаследовал jackpot-ветку
    # post_slots по ошибке.
    assert "jackpot" not in body
    # `bank_capped` — БЕЗУСЛОВНО, включая проигрышный спин (у Тето нет
    # отдельного признака победы, см. модульный докстринг api/routes/games.py):
    # отсутствие ключа клиенту пришлось бы отличать от `false`.
    assert body["bank_capped"] in (True, False)


@pytest.mark.asyncio
async def test_teto_slots_missing_init_data_returns_401():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/games/teto_slots",
            params={"chat_id": TETO_SLOTS_CHAT_ID},
            json={"bet": 10 * teto_slot_engine.TOTAL_LINES, "idem_key": str(uuid.uuid4())},
        )

    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_teto_slots_bet_below_minimum_returns_400(monkeypatch):
    """bet=1 нарушает ОБА ограничения play_teto_slots: ниже casino_min_bet И
    не кратно teto_slot_engine.TOTAL_LINES — оба пути ведут к
    InvalidBet->400 (тот же приём, что test_slots_bet_below_minimum_returns_400
    выше)."""
    monkeypatch.setattr(telegram_client, "get_chat_member_status", AsyncMock(return_value="member"))
    user_id = 300421
    await _ensure_user(user_id)
    await _topup(TETO_SLOTS_CHAT_ID, user_id)
    init_data = _build_init_data(user_id=user_id)
    assert 1 < settings.casino_min_bet
    assert 1 % teto_slot_engine.TOTAL_LINES != 0

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/games/teto_slots",
            params={"chat_id": TETO_SLOTS_CHAT_ID},
            headers={"X-Telegram-Init-Data": init_data},
            json={"bet": 1, "idem_key": str(uuid.uuid4())},
        )

    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_teto_slots_bet_not_multiple_of_lines_returns_400(monkeypatch):
    monkeypatch.setattr(telegram_client, "get_chat_member_status", AsyncMock(return_value="member"))
    user_id = 300422
    await _ensure_user(user_id)
    await _topup(TETO_SLOTS_CHAT_ID, user_id)
    init_data = _build_init_data(user_id=user_id)
    bet = settings.casino_min_bet + 1
    assert bet >= settings.casino_min_bet
    assert bet % teto_slot_engine.TOTAL_LINES != 0

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/games/teto_slots",
            params={"chat_id": TETO_SLOTS_CHAT_ID},
            headers={"X-Telegram-Init-Data": init_data},
            json={"bet": bet, "idem_key": str(uuid.uuid4())},
        )

    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_teto_slots_rapid_second_spin_returns_429(monkeypatch):
    """Троттлинг-дикт `_last_slots_spin_at` общий для Azumanga и Тето (см.
    докстринг `casino_service._check_slots_throttle`) — тот же сброс, что и
    test_slots_rapid_second_spin_returns_429 выше."""
    monkeypatch.setattr(telegram_client, "get_chat_member_status", AsyncMock(return_value="member"))
    monkeypatch.setattr(casino_service, "_last_slots_spin_at", {})
    user_id = 300423
    await _ensure_user(user_id)
    await _topup(TETO_SLOTS_CHAT_ID, user_id)
    init_data = _build_init_data(user_id=user_id)
    bet = 10 * teto_slot_engine.TOTAL_LINES

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        first = await client.post(
            "/api/v1/games/teto_slots",
            params={"chat_id": TETO_SLOTS_CHAT_ID},
            headers={"X-Telegram-Init-Data": init_data},
            json={"bet": bet, "idem_key": str(uuid.uuid4())},
        )
        second = await client.post(
            "/api/v1/games/teto_slots",
            params={"chat_id": TETO_SLOTS_CHAT_ID},
            headers={"X-Telegram-Init-Data": init_data},
            json={"bet": bet, "idem_key": str(uuid.uuid4())},
        )

    assert first.status_code == 200
    assert second.status_code == 429


@pytest.mark.asyncio
async def test_teto_slots_response_carries_animation_and_null_on_replay(monkeypatch):
    """Контракт ключа `animation` на уровне HTTP (см. модульный докстринг
    `api/routes/games.py`): на свежем спине — конверт с `version`/`ops`/
    `truncated`, первый кадр `fill`; на POST с ТЕМ ЖЕ `idem_key` (сетевой
    ретрай/replay) — `null`, и это НОРМАЛЬНЫЙ 200-й ответ, а не ошибка.

    Почему это стоит проверять именно здесь, а не только на уровне сервиса:
    роут — единственное место, где пустой sink превращается в `null`, где
    стоит `animation.clear()` перед каждой попыткой, и единственное место, где
    весь пейлоад реально проходит JSON-сериализацию FastAPI. Фронт, который
    трактует отсутствие анимации как ошибку, покажет игроку пустую доску после
    ретрая запроса — ровно этот сценарий тест и фиксирует как штатный."""
    monkeypatch.setattr(telegram_client, "get_chat_member_status", AsyncMock(return_value="member"))
    monkeypatch.setattr(casino_service, "_last_slots_spin_at", {})
    user_id = 300426
    await _ensure_user(user_id)
    await _topup(TETO_SLOTS_CHAT_ID, user_id)
    init_data = _build_init_data(user_id=user_id)
    bet = 10 * teto_slot_engine.TOTAL_LINES
    idem_key = str(uuid.uuid4())

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        first = await client.post(
            "/api/v1/games/teto_slots",
            params={"chat_id": TETO_SLOTS_CHAT_ID},
            headers={"X-Telegram-Init-Data": init_data},
            json={"bet": bet, "idem_key": idem_key},
        )
        # ТОТ ЖЕ idem_key: replay уже settled раунда — не троттлится (гард
        # `is_new_spin`), деньги не двигаются повторно, анимации нет.
        replay = await client.post(
            "/api/v1/games/teto_slots",
            params={"chat_id": TETO_SLOTS_CHAT_ID},
            headers={"X-Telegram-Init-Data": init_data},
            json={"bet": bet, "idem_key": idem_key},
        )

    assert first.status_code == 200
    body = first.json()
    animation = body["animation"]
    assert animation is not None, "на свежем спине анимация обязана быть"
    assert animation["version"] == teto_slot_engine.TRACE_SCHEMA_VERSION
    assert animation["truncated"] is False
    assert animation["truncated_reason"] is None
    assert animation["ops_recorded"] == animation["ops_total"] == len(animation["ops"])
    assert animation["rounds_recorded"] == animation["rounds_total"] >= 1
    # Базовый раунд — round 0 (ЛОЖЕН в JS: сверять с null, не по truthiness).
    assert animation["complete_through_round"] == animation["rounds_recorded"] - 1
    assert animation["ops"][0]["op"] == "fill"
    assert animation["ops"][0]["round"] == 0
    assert animation["ops"][0]["phase"] == "base"
    assert animation["ops"][-1]["op"] == "round_end"
    # Кадры трейса — та же кодировка доски, что и outcome.final_blocks.
    assert animation["ops"][0]["blocks"] == body["outcome"]["initial_blocks"]
    for op in animation["ops"]:
        assert op["op"] in ("fill", "evaluate", "tumble", "drill_hunt", "round_end")
        assert len(op["blocks"]) >= 1

    # Лестница для шкалы-вала: статика в конверте, цель — в round_end.
    assert animation["ladder_max_score"] == 36
    assert [t["multiplier"] for t in animation["ladder_thresholds"]] == [2, 3, 5, 10]
    for op in animation["ops"]:
        if op["op"] == "round_end" and op["ladder"] is not None:
            assert {"next_threshold", "next_multiplier", "score_to_next"} <= set(op["ladder"])

    assert replay.status_code == 200
    replay_body = replay.json()
    assert replay_body["animation"] is None, "replay обязан отдавать animation: null, а не {}"
    assert replay_body["payout"] == body["payout"]
    assert replay_body["outcome"] == body["outcome"]
    # `bank_capped` считается из `payout`/`outcome.total_payout`, поэтому есть
    # и на replay, где анимации нет вовсе — клиенту не нужно различать эти
    # случаи, чтобы честно подписать урезанный выигрыш.
    assert replay_body["bank_capped"] == body["bank_capped"]


async def _drain_bank(chat_id: int) -> None:
    """Обнуляет `chat_bank.balance` конкретного чата ПЕРЕД спином.

    Нужен, потому что Postgres тестов долгоживущий (см. `_topup`): банк
    "свежего" chat_id перестаёт быть свежим после первого же прогона файла, а
    вся суть теста ниже — в том, что банку НЕЧЕМ платить. Пишем напрямую в
    таблицу, а не через `economy_service`: это сетап окружения, а не денежная
    операция, и `economy_service` намеренно не имеет примитива "обнулить банк".
    """
    async with SessionLocal() as db_session:
        await db_session.execute(
            update(ChatBank).where(ChatBank.chat_id == chat_id).values(balance=0)
        )
        await db_session.commit()


@pytest.mark.asyncio
async def test_teto_slots_win_on_drained_bank_reports_bank_capped_and_animation_agrees(monkeypatch):
    """ГЛАВНАЯ регрессия анимации: экран НЕ ДОЛЖЕН досчитывать счётчик до
    суммы, которой игрок не получил.

    Тот же инцидент, что уже был на Azumanga (см. `bank_capped` в модульном
    докстринге `api/routes/games.py`: "/me до и после раунда — 1000 и 1000,
    банк чата был 0"), но у Тето он громче: там фронт просто бампал число, а
    здесь он ТИКАЕТ счётчик по раундам из `animation` и грейдит большой
    выигрыш. Сценарий не экзотический — 10.8% реальных спинов платят больше
    ставки (замер на 20 000 спинов, максимум 126x ставки), а банк свежего или
    выеденного чата этого не тянет.

    Форс полностью детерминирован: `casino_service._rng` подменён на
    `random.Random(12945)` — движок берёт RNG ТОЛЬКО оттуда (D-03/T-04.1-01,
    джекпот-слоя у Тето нет вовсе), поэтому спин воспроизводится побайтово и
    честно платит 3 790 при ставке 30. Банк чата обнулён, в него попадает
    ровно ставка -> `pay_from_bank` (D-06) платит 30 из 3 790.

    Что именно фиксируем как клиентский контракт:
      - `payout` == 30, а `outcome.total_payout` == 3 790: аудиторская запись
        НЕ фальсифицируется под выплату (иначе мы бы соврали в другую сторону);
      - `bank_capped: true` на верхнем уровне — клиенту не нужно ничего
        вычитать самому, чтобы понять, что произошло;
      - `animation.payout_paid` == `payout` и `animation.bank_capped` == true —
        компоненту счётчика не нужно ходить за числом в соседнюю ветку ответа;
      - сумма `final_round_payout` по раундам трейса РАВНА неурезанному итогу
        (движок не знает про банк) и БОЛЬШЕ выплаты — то есть наивный
        счётчик действительно соврал бы, и формула `min(префикс, payout_paid)`
        нужна не теоретически;
      - баланс игрока не изменился вовсе (`-30` ставки `+30` выплаты) — та
        самая подпись инцидента, которую игрок читает как "экран соврал"."""
    monkeypatch.setattr(telegram_client, "get_chat_member_status", AsyncMock(return_value="member"))
    monkeypatch.setattr(casino_service, "_last_slots_spin_at", {})
    monkeypatch.setattr(casino_service, "_rng", random.Random(12945))
    user_id = 300427
    await _ensure_user(user_id)
    await _topup(TETO_DRAINED_BANK_CHAT_ID, user_id)
    await _drain_bank(TETO_DRAINED_BANK_CHAT_ID)
    init_data = _build_init_data(user_id=user_id)
    bet = 10 * teto_slot_engine.TOTAL_LINES

    balance_before = await _get_balance(TETO_DRAINED_BANK_CHAT_ID, user_id)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/games/teto_slots",
            params={"chat_id": TETO_DRAINED_BANK_CHAT_ID},
            headers={"X-Telegram-Init-Data": init_data},
            json={"bet": bet, "idem_key": str(uuid.uuid4())},
        )

    assert resp.status_code == 200
    body = resp.json()

    engine_total = body["outcome"]["total_payout"]
    assert engine_total > bet, (
        f"форсированный сид обязан выиграть больше ставки, чтобы кап наступил "
        f"(получено {engine_total} при ставке {bet}) — если движок изменился, подобрать новый сид"
    )
    assert body["payout"] == bet, "банк содержал ровно ставку этого же спина — больше платить нечем"
    assert body["bank_capped"] is True
    assert body["user_balance_after"] == balance_before, (
        "подпись инцидента: игрок выиграл, а баланс не изменился вовсе"
    )

    animation = body["animation"]
    assert animation is not None
    assert animation["payout_paid"] == body["payout"]
    assert animation["payout_engine_total"] == engine_total
    assert animation["bank_capped"] is True

    # Наивный счётчик (сумма раундов) действительно уехал бы выше выплаты...
    naive_total = sum(op["final_round_payout"] for op in animation["ops"] if op["op"] == "round_end")
    assert naive_total == engine_total > animation["payout_paid"]

    # ...а предписанная контрактом формула `min(префикс, payout_paid)` — нет:
    # монотонна, не превышает выплату и заканчивается ровно на ней.
    running = 0
    shown = 0
    for op in animation["ops"]:
        if op["op"] != "round_end":
            continue
        running += op["final_round_payout"]
        step = min(running, animation["payout_paid"])
        assert step >= shown, "счётчик обязан быть монотонным"
        assert step <= body["payout"], "счётчик перевалил за реально выплаченное"
        shown = step
    assert shown == body["payout"]


@pytest.mark.asyncio
async def test_teto_slots_win_on_funded_bank_reports_bank_capped_false(monkeypatch):
    """Обратная сторона: тот же форсированный выигрышный спин, но банк чата
    заведомо богат — `bank_capped: false`, выплата равна честному итогу, и
    `animation.payout_paid` совпадает с обоими.

    Без этой половины `bank_capped: true` было бы неотличимо от константы:
    флаг, который всегда `true`, клиент так же уверенно проигнорирует, как и
    отсутствующий."""
    monkeypatch.setattr(telegram_client, "get_chat_member_status", AsyncMock(return_value="member"))
    monkeypatch.setattr(casino_service, "_last_slots_spin_at", {})
    monkeypatch.setattr(casino_service, "_rng", random.Random(12945))
    user_id = 300428
    await _ensure_user(user_id)
    await _topup(TETO_SLOTS_CHAT_ID, user_id)
    init_data = _build_init_data(user_id=user_id)
    bet = 10 * teto_slot_engine.TOTAL_LINES

    async with SessionLocal() as db_session:
        await economy_service.credit_bank(
            db_session, TETO_SLOTS_CHAT_ID, 1_000_000,
            kind="test_seed", ref_id=f"test_teto_funded_bank:{uuid.uuid4()}",
        )
        await db_session.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/games/teto_slots",
            params={"chat_id": TETO_SLOTS_CHAT_ID},
            headers={"X-Telegram-Init-Data": init_data},
            json={"bet": bet, "idem_key": str(uuid.uuid4())},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["outcome"]["total_payout"] > bet
    assert body["payout"] == body["outcome"]["total_payout"], "богатый банк платит честный итог целиком"
    assert body["bank_capped"] is False
    assert body["animation"]["bank_capped"] is False
    assert body["animation"]["payout_paid"] == body["payout"]
    assert body["animation"]["payout_engine_total"] == body["payout"]


@pytest.mark.asyncio
async def test_teto_slots_ignores_foreign_user_id_in_body_idor(monkeypatch):
    """T-04.2-02: та же IDOR-защита, что и у остальных игр этого файла."""
    monkeypatch.setattr(telegram_client, "get_chat_member_status", AsyncMock(return_value="member"))
    attacker_id = 300424
    victim_id = 300425
    await _ensure_user(attacker_id)
    await _topup(TETO_SLOTS_CHAT_ID, attacker_id)
    await _ensure_user(victim_id)
    await _topup(TETO_SLOTS_CHAT_ID, victim_id)
    init_data = _build_init_data(user_id=attacker_id)
    idem_key = str(uuid.uuid4())
    bet = 10 * teto_slot_engine.TOTAL_LINES

    victim_before = await _get_balance(TETO_SLOTS_CHAT_ID, victim_id)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/games/teto_slots",
            params={"chat_id": TETO_SLOTS_CHAT_ID},
            headers={"X-Telegram-Init-Data": init_data},
            json={
                "bet": bet,
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

    victim_after = await _get_balance(TETO_SLOTS_CHAT_ID, victim_id)
    assert victim_after == victim_before


# --- GET /api/v1/games/slots/jackpot (CASINO-06) ------------------------------


@pytest.mark.asyncio
async def test_jackpot_pool_get_returns_seed_for_fresh_chat(monkeypatch):
    monkeypatch.setattr(telegram_client, "get_chat_member_status", AsyncMock(return_value="member"))
    user_id = 300406
    await _ensure_user(user_id)
    init_data = _build_init_data(user_id=user_id)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            "/api/v1/games/slots/jackpot",
            params={"chat_id": JACKPOT_CHAT_ID},
            headers={"X-Telegram-Init-Data": init_data},
        )

    assert resp.status_code == 200
    assert resp.json() == {"pool": settings.slot_jackpot_seed}


@pytest.mark.asyncio
async def test_jackpot_pool_get_reflects_growth_after_spin(monkeypatch):
    monkeypatch.setattr(telegram_client, "get_chat_member_status", AsyncMock(return_value="member"))
    user_id = 300407
    await _ensure_user(user_id)
    await _topup(JACKPOT_CHAT_ID, user_id)
    init_data = _build_init_data(user_id=user_id)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        spin_resp = await client.post(
            "/api/v1/games/slots",
            params={"chat_id": JACKPOT_CHAT_ID},
            headers={"X-Telegram-Init-Data": init_data},
            json={"bet": 100, "idem_key": str(uuid.uuid4())},
        )
        assert spin_resp.status_code == 200
        pool_after_spin = spin_resp.json()["jackpot"]["pool"]

        pool_resp = await client.get(
            "/api/v1/games/slots/jackpot",
            params={"chat_id": JACKPOT_CHAT_ID},
            headers={"X-Telegram-Init-Data": init_data},
        )

    assert pool_resp.status_code == 200
    assert pool_resp.json() == {"pool": pool_after_spin}


@pytest.mark.asyncio
async def test_jackpot_pool_get_missing_init_data_returns_401():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            "/api/v1/games/slots/jackpot", params={"chat_id": JACKPOT_CHAT_ID}
        )

    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_slots_jackpot_win_announces_gif_to_chat(monkeypatch):
    """CASINO-06: срыв джекпота -> `telegram_client.send_animation` уходит в
    ИМЕННО тот чат, где играли, с подписью, несущей выплаченную сумму."""
    monkeypatch.setattr(telegram_client, "get_chat_member_status", AsyncMock(return_value="member"))
    send_animation_mock = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr(telegram_client, "send_animation", send_animation_mock)
    forced_symbols = ["sakaki", "bath-chibi", "osaka-stand", "dog", "gasp"] * 3
    monkeypatch.setattr(casino_service, "_rng", _ForcedJackpotGridRng(forced_symbols))

    user_id = 300408
    await _ensure_user(user_id)
    await _topup(JACKPOT_CHAT_ID, user_id)
    init_data = _build_init_data(user_id=user_id)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/games/slots",
            params={"chat_id": JACKPOT_CHAT_ID},
            headers={"X-Telegram-Init-Data": init_data},
            json={"bet": 100, "idem_key": str(uuid.uuid4())},
        )

    assert resp.status_code == 200
    jackpot = resp.json()["jackpot"]
    assert jackpot["won"] is True

    send_animation_mock.assert_awaited_once()
    call_args = send_animation_mock.await_args.args
    assert call_args[2] == JACKPOT_CHAT_ID
    caption = send_animation_mock.await_args.kwargs["caption"]
    assert str(jackpot["amount"]) in caption


@pytest.mark.asyncio
async def test_slots_jackpot_loss_does_not_announce(monkeypatch):
    """Обычный (не джекпотный) спин НЕ шлёт гифку в чат."""
    monkeypatch.setattr(telegram_client, "get_chat_member_status", AsyncMock(return_value="member"))
    send_animation_mock = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr(telegram_client, "send_animation", send_animation_mock)
    forced_symbols = ["sakaki", "bath-chibi", "osaka-stand", "dog", "gasp"] * 3
    monkeypatch.setattr(
        casino_service, "_rng", _ForcedJackpotGridRng(forced_symbols, jackpot_randint=2)
    )

    user_id = 300409
    await _ensure_user(user_id)
    await _topup(JACKPOT_CHAT_ID, user_id)
    init_data = _build_init_data(user_id=user_id)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/games/slots",
            params={"chat_id": JACKPOT_CHAT_ID},
            headers={"X-Telegram-Init-Data": init_data},
            json={"bet": 100, "idem_key": str(uuid.uuid4())},
        )

    assert resp.status_code == 200
    assert resp.json()["jackpot"]["won"] is False
    send_animation_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_slots_jackpot_win_with_drained_bank_does_not_announce(monkeypatch):
    """Баг-регрессия: тот же спин одновременно (а) срывает джекпот-кубик
    (RNG) и (б) выдаёт честный line-win (muscle wild на всех 15 клетках =
    максимально возможная выплата, заведомо превышающая любой банк) — этот
    line-win выплачивается ПЕРВЫМ (внутри `_settle`) и полностью выедает
    chat_bank (свежий чат, банк = только что зачисленная ставка) ДО того, как
    `jackpot_service` пытается заплатить пул. К моменту джекпот-ролла
    bank_balance == 0 -> `pay_from_bank` платит 0. Роут не должен публиковать
    анонс "+0¥"."""
    monkeypatch.setattr(telegram_client, "get_chat_member_status", AsyncMock(return_value="member"))
    send_animation_mock = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr(telegram_client, "send_animation", send_animation_mock)
    forced_symbols = ["muscle"] * 15
    monkeypatch.setattr(casino_service, "_rng", _ForcedJackpotGridRng(forced_symbols))

    user_id = 300411
    await _ensure_user(user_id)
    await _topup(JACKPOT_EMPTY_BANK_CHAT_ID, user_id)
    init_data = _build_init_data(user_id=user_id)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/games/slots",
            params={"chat_id": JACKPOT_EMPTY_BANK_CHAT_ID},
            headers={"X-Telegram-Init-Data": init_data},
            json={"bet": 10 * slot_engine.TOTAL_LINES, "idem_key": str(uuid.uuid4())},
        )

    assert resp.status_code == 200
    jackpot = resp.json()["jackpot"]
    assert jackpot["won"] is True
    assert jackpot["amount"] == 0  # банк уже выеден выплатой честного line-win
    send_animation_mock.assert_not_awaited()


# --- POST /api/v1/games/blackjack (start) + /blackjack/{id}/action -----------
# (Task 1/2, 04.2-10) ---------------------------------------------------------


@pytest.mark.asyncio
async def test_blackjack_start_valid_bet_returns_200_with_active_hand(monkeypatch):
    monkeypatch.setattr(telegram_client, "get_chat_member_status", AsyncMock(return_value="member"))
    # 8+4 = 12 (не натурал) — раздача остаётся "active", удобно проверить
    # ровно форму start-ответа без немедленного settle.
    monkeypatch.setattr(casino_service, "_rng", _FixedDeckRng(["8♠", "4♠", "7♠", "5♠"]))
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
    assert body["player"] == ["8♠", "4♠"]
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
        monkeypatch.setattr(casino_service, "_rng", _FixedDeckRng(["8♠", "4♠", "7♠", "5♠", "3♠"]))
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
    assert body["player"] == ["8♠", "4♠", "3♠"]

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
        monkeypatch.setattr(casino_service, "_rng", _FixedDeckRng(["8♠", "4♠", "7♠", "5♠", "9♠"]))
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
    assert body["dealer"] == ["7♠", "5♠", "9♠"]
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
            casino_service, "_rng", _FixedDeckRng(["8♠", "4♠", "7♠", "5♠", "2♠", "6♠"])
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
    assert body["player"] == ["8♠", "4♠", "2♠"]
    assert body["dealer"] == ["7♠", "5♠", "6♠"]
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
        monkeypatch.setattr(casino_service, "_rng", _FixedDeckRng(["8♠", "4♠", "7♠", "5♠", "3♠"]))
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
        monkeypatch.setattr(casino_service, "_rng", _FixedDeckRng(["8♠", "4♠", "7♠", "5♠"]))
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
        monkeypatch.setattr(casino_service, "_rng", _FixedDeckRng(["8♠", "4♠", "7♠", "5♠"]))
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
async def test_blackjack_action_double_with_foreign_chat_id_returns_404(monkeypatch):
    """Раздача открыта в BLACKJACK_CHAT_ID (чат A); тот же игрок пытается
    выполнить `double`, подставив chat_id ДРУГОГО чата (B), где он тоже
    участник. Раньше SELECT в `blackjack_action` фильтровал только по
    user_id/game_id (не chat_id) — это находило раздачу чата A, а `double`
    списывал вторую ставку из банка чата B (`_debit_stake(session, chat_id,
    ...)`), пока выплата всё равно уходила в банк чата A (`game_row.chat_id`)
    — банк A рос за чужой счёт. Теперь такая раздача структурно неотличима
    от несуществующей -> 404, ни раздача, ни балансы обоих чатов не меняются."""
    monkeypatch.setattr(telegram_client, "get_chat_member_status", AsyncMock(return_value="member"))
    other_chat_id = -900555
    user_id = 300512
    await _ensure_user(user_id)
    await _topup(BLACKJACK_CHAT_ID, user_id)
    await _topup(other_chat_id, user_id)
    init_data = _build_init_data(user_id=user_id)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        monkeypatch.setattr(casino_service, "_rng", _FixedDeckRng(["8♠", "4♠", "7♠", "5♠"]))
        game_id = await _start_fixed_hand(client, init_data, BLACKJACK_CHAT_ID, [])
        balance_a_before = await _get_balance(BLACKJACK_CHAT_ID, user_id)
        balance_b_before = await _get_balance(other_chat_id, user_id)

        resp = await client.post(
            f"/api/v1/games/blackjack/{game_id}/action",
            params={"chat_id": other_chat_id},
            headers={"X-Telegram-Init-Data": init_data},
            json={"action": "double"},
        )

    assert resp.status_code == 404

    async with SessionLocal() as verify_session:
        game_row = (
            await verify_session.execute(select(CasinoGame).where(CasinoGame.id == game_id))
        ).scalar_one()
    assert game_row.status == "active"
    assert game_row.chat_id == BLACKJACK_CHAT_ID

    assert await _get_balance(BLACKJACK_CHAT_ID, user_id) == balance_a_before
    assert await _get_balance(other_chat_id, user_id) == balance_b_before

    await _force_settle_leftover_game(game_id)


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
        monkeypatch.setattr(casino_service, "_rng", _FixedDeckRng(["8♠", "4♠", "7♠", "5♠", "9♠"]))
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
