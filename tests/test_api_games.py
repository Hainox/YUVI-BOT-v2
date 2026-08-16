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
from datetime import datetime
from datetime import timedelta
from decimal import Decimal
from unittest.mock import AsyncMock
from urllib.parse import urlencode

import pytest
from httpx import ASGITransport
from httpx import AsyncClient
from sqlalchemy import delete
from sqlalchemy import select
from sqlalchemy import update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from api import telegram_client
from api.main import app
from bot.config import settings
from bot.services import casino_service
from bot.services import economy_service
from bot.services import jackpot_service
from bot.services import slot_engine
from bot.services import teto_slot_engine
from common.db.session import engine
from common.db.session import SessionLocal
from common.models.casino_game import CasinoGame
from common.models.chat_bank import ChatBank
from common.models.slot_jackpot import SlotJackpot
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

# Отдельные chat_id ТОЛЬКО под bank_capped-регрессию блэкджека (D-06) — та же
# изоляция, что FRESH_BANK_CHAT_ID у coinflip: банк каждого из этих чатов не
# делится ни с BLACKJACK_CHAT_ID (остальные блэкджек-тесты выше), ни друг с
# другом, иначе накопленные ставки соседей случайно покрыли бы выигрыш и кап
# не наступил бы (или наоборот — "богатый банк" тест смешался бы со свежим).
BLACKJACK_FRESH_BANK_CHAT_ID = -900313  # свежий (нулевой) банк, натурал через /start
BLACKJACK_ACTION_FRESH_BANK_CHAT_ID = -900314  # свежий банк, выигрыш через /action (stand)
BLACKJACK_FUNDED_BANK_CHAT_ID = -900315  # заведомо богатый банк, double через /action

# Отдельные chat_id для POST /games/crash + /games/crash/{id}/cashout — та же
# изоляция, что у остальных игр этого файла, новый диапазон.
CRASH_CHAT_ID = -900316
# Заведомо богатый банк — полный старт-кэшаут раунд-трип не должен упереться
# в D-06-кап (та же роль, что BLACKJACK_FUNDED_BANK_CHAT_ID выше).
CRASH_FUNDED_BANK_CHAT_ID = -900317
# Свежий (нулевой) банк ТОЛЬКО под bank_capped-регрессию кэшаута — та же
# изоляция, что FRESH_BANK_CHAT_ID у coinflip/BLACKJACK_FRESH_BANK_CHAT_ID
# выше: банк этого чата не делится ни с одним другим тестом файла.
CRASH_FRESH_BANK_CHAT_ID = -900318

# Отдельные chat_id для джекпот-ивента (/jackpot_event, CASINO-06 follow-up) —
# та же изоляция, что у остальных диапазонов этого файла.
JACKPOT_EVENT_CHAT_ID = -900319
JACKPOT_EVENT_EMPTY_BANK_CHAT_ID = -900320


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


class _ForcedCrashRng:
    """Форсирует детерминированную (скрытую) точку краша — `_rng.random()`
    внутри `crash_engine.generate_crash_point` всегда возвращает фиксированное
    `forced_r` (см. его докстринг: `r < CRASH_HOUSE_EDGE` (0.02) -> мгновенный
    краш на 1.00x, иначе `crash_point = (1-edge)/(1-r)`). `forced_r=0.99` даёт
    `crash_point=98.00` (заведомо "долгоживущий" раунд — любой кэшаут в первые
    секунды реального времени — выигрыш); `forced_r=0.0` даёт мгновенный краш
    1.00x (гарантированный проигрыш даже при кэшауте в саму первую миллисекунду,
    тот же форсированный instant-bust, что `tests/test_casino_service.py`'s
    crash-секция использует на уровне сервиса)."""

    def __init__(self, forced_r: float):
        self._forced_r = forced_r

    def random(self) -> float:
        return self._forced_r


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
    выигрыш. Сценарий не экзотический — ~29.5% реальных спинов платят больше
    ставки (замер на 100 000 спинов ПОСЛЕ перекалибровки от 2026-08-06,
    максимум наблюдался ~258x ставки в этой выборке, официальная калибровка
    на 300k видела хвост вплоть до ~300x — доля и хвост заметно выросли
    против дореформенных цифр из-за смены `count_scatter_blocks` на
    сумму площадей, см. её докстринг в `teto_tumble.py`), а банк свежего или
    выеденного чата этого не тянет.

    Форс полностью детерминирован: `casino_service._rng` подменён на
    `random.Random(6)` (был `random.Random(12945)` — перестал платить больше
    ставки после перекалибровки WEIGHTS/TETO_PAYTABLE, сдвинувшей потребление
    RNG базового заполнения; переподобран как первый сид от 0, чей
    `total_payout` при этой ставке превышает саму ставку) — движок берёт RNG
    ТОЛЬКО оттуда (D-03/T-04.1-01, джекпот-слоя у Тето нет вовсе), поэтому
    спин воспроизводится побайтово и честно платит 1 430 при ставке 500
    (`bet = 10 * TOTAL_LINES`). Банк чата обнулён, в него попадает ровно
    ставка -> `pay_from_bank` (D-06) платит 500 из 1 430.

    Что именно фиксируем как клиентский контракт:
      - `payout` == 500, а `outcome.total_payout` == 1 430: аудиторская
        запись НЕ фальсифицируется под выплату (иначе мы бы соврали в другую
        сторону);
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
    monkeypatch.setattr(casino_service, "_rng", random.Random(6))
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
    monkeypatch.setattr(casino_service, "_rng", random.Random(6))
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
    # Пул живёт в test-БД МЕЖДУ прогонами (compose test-профиль переиспользует
    # volume pgdata_test, CI получает свежую БД на каждый job), а тест требует
    # "свежий" чат с пулом ровно = seed — чистим свою строку, чтобы повторный
    # локальный прогон не падал (найдено 2026-08-16: 2-й прогон видел pool=1003
    # вместо 1000 из-за скима спинов прошлого прогона).
    async with SessionLocal() as db_session:
        await db_session.execute(delete(SlotJackpot).where(SlotJackpot.chat_id == JACKPOT_CHAT_ID))
        await db_session.commit()

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


# --- Джекпот-ивент "ГИФТЕКИ ОТ ОСАКИ" — announce-путь через реальный роут ----


@pytest.mark.asyncio
async def test_slots_jackpot_event_win_announces_with_event_caption(monkeypatch):
    """Активный ивент (spins=1 -> remaining==1 -> гарантированный выигрыш на
    первом же спине, независимо от jackpot_randint) даёт `event_win=True` в
    ответе и шлёт в чат подпись именно с EVENT_NAME, не обычную."""
    monkeypatch.setattr(telegram_client, "get_chat_member_status", AsyncMock(return_value="member"))
    send_animation_mock = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr(telegram_client, "send_animation", send_animation_mock)
    forced_symbols = ["sakaki", "bath-chibi", "osaka-stand", "dog", "gasp"] * 3
    # jackpot_randint=999 -> в ОБЫЧНОМ режиме гарантированный проигрыш;
    # доказывает, что выигрыш пришёл именно от pity-короткого замыкания
    # remaining==1, а не от рандома.
    monkeypatch.setattr(
        casino_service, "_rng", _ForcedJackpotGridRng(forced_symbols, jackpot_randint=999)
    )

    user_id = 300412
    await _ensure_user(user_id)
    await _topup(JACKPOT_EVENT_CHAT_ID, user_id)
    init_data = _build_init_data(user_id=user_id)

    async with SessionLocal() as db_session:
        await jackpot_service.start_event(db_session, JACKPOT_EVENT_CHAT_ID, 1)
        await db_session.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/games/slots",
            params={"chat_id": JACKPOT_EVENT_CHAT_ID},
            headers={"X-Telegram-Init-Data": init_data},
            json={"bet": 100, "idem_key": str(uuid.uuid4())},
        )

    assert resp.status_code == 200
    jackpot = resp.json()["jackpot"]
    assert jackpot["won"] is True
    assert jackpot["event_win"] is True

    send_animation_mock.assert_awaited_once()
    caption = send_animation_mock.await_args.kwargs["caption"]
    assert jackpot_service.EVENT_NAME in caption
    assert str(jackpot["amount"]) in caption


@pytest.mark.asyncio
async def test_slots_jackpot_event_win_with_drained_bank_still_announces(monkeypatch):
    """Регрессия найдена адверсариальным ревью 2026-08-07: обычный (не-
    ивентный) джекпот с нулевой выплатой молчит (см.
    test_slots_jackpot_win_with_drained_bank_does_not_announce выше) — но
    ивент-выигрыш ОБЯЗАН объявиться в чат ДАЖЕ с нулевой выплатой, иначе
    ивент "сгорал" бы незаметно для чата, что противоречит всему его смыслу
    ("первый, кто поймает — заберёт банк и объявление увидят все")."""
    monkeypatch.setattr(telegram_client, "get_chat_member_status", AsyncMock(return_value="member"))
    send_animation_mock = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr(telegram_client, "send_animation", send_animation_mock)
    forced_symbols = ["muscle"] * 15  # честный line-win съедает банк ДО джекпот-ролла
    monkeypatch.setattr(
        casino_service, "_rng", _ForcedJackpotGridRng(forced_symbols, jackpot_randint=999)
    )

    user_id = 300413
    await _ensure_user(user_id)
    await _topup(JACKPOT_EVENT_EMPTY_BANK_CHAT_ID, user_id)
    init_data = _build_init_data(user_id=user_id)

    async with SessionLocal() as db_session:
        await jackpot_service.start_event(db_session, JACKPOT_EVENT_EMPTY_BANK_CHAT_ID, 1)
        await db_session.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/games/slots",
            params={"chat_id": JACKPOT_EVENT_EMPTY_BANK_CHAT_ID},
            headers={"X-Telegram-Init-Data": init_data},
            json={"bet": 10 * slot_engine.TOTAL_LINES, "idem_key": str(uuid.uuid4())},
        )

    assert resp.status_code == 200
    jackpot = resp.json()["jackpot"]
    assert jackpot["won"] is True
    assert jackpot["event_win"] is True
    assert jackpot["amount"] == 0  # банк уже выеден честным line-win

    send_animation_mock.assert_awaited_once()
    caption = send_animation_mock.await_args.kwargs["caption"]
    assert jackpot_service.EVENT_NAME in caption
    assert "пуст" in caption.lower()  # особая формулировка для нулевой выплаты, не "обнулён"


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
    # Раздача ещё не settle'илась (не натурал) — bank_capped обязан быть
    # честным False, а не отсутствующим ключом (см. bank_capped-комментарий
    # post_blackjack_start в api/routes/games.py).
    assert body["bank_capped"] is False

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
    # Честный payout проигрыша — тоже 0 (fair_mult=0.0 для "lose"), поэтому
    # bank_capped обязан остаться False, а не стать True из-за "0 < 0".
    assert body["bank_capped"] is False


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


@pytest.mark.asyncio
async def test_blackjack_start_natural_win_on_empty_bank_reports_bank_capped(monkeypatch):
    """Регрессия по тому же классу инцидента, что и `test_coinflip_win_on_
    empty_bank_reports_bank_capped` выше (см. модульный докстринг
    api/routes/games.py) — только для блэкджека и через немедленный settle
    натурала внутри `post_blackjack_start` (не /action). Свежий (нулевой)
    chat_bank чата пополняется РОВНО ставкой самой же раздачей (`_debit_stake`)
    до выплаты — честный натурал (bet * 2.5) не влезает, capped-выплата
    урезается до размера самой ставки. `casino_service.start_blackjack`'ы
    тест-эквивалент — `tests/test_blackjack_service.py::
    test_payout_capped_to_bank` (та же форсированная колода)."""
    monkeypatch.setattr(telegram_client, "get_chat_member_status", AsyncMock(return_value="member"))
    # player=[A,K]=21 (натурал), dealer=[8,4]=12 (не натурал) -> "natural", 2.5x.
    monkeypatch.setattr(casino_service, "_rng", _FixedDeckRng(["A♠", "K♠", "8♠", "4♠"]))
    user_id = 300513
    await _ensure_user(user_id)
    init_data = _build_init_data(user_id=user_id)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/games/blackjack",
            params={"chat_id": BLACKJACK_FRESH_BANK_CHAT_ID},
            headers={"X-Telegram-Init-Data": init_data},
            json={"bet": 100, "idem_key": str(uuid.uuid4())},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "settled"
    assert body["outcome"]["result"] == "natural"
    # Честный натурал — 250 (100 * 2.5); банк стартовал с 0, пополнился
    # только ставкой (100) до выплаты — capped-payout == ставке.
    assert body["payout"] == 100
    assert body["payout"] < int(100 * casino_service.BLACKJACK_NATURAL_MULT)
    assert body["bank_capped"] is True


@pytest.mark.asyncio
async def test_blackjack_action_stand_win_on_empty_bank_reports_bank_capped(monkeypatch):
    """Тот же D-06-инцидент, что и в тесте выше, но через POST /blackjack/
    {game_id}/action ("stand"), не немедленный settle натурала на /start —
    покрывает bank_capped-вычисление именно в post_blackjack_action."""
    monkeypatch.setattr(telegram_client, "get_chat_member_status", AsyncMock(return_value="member"))
    user_id = 300514
    await _ensure_user(user_id)
    init_data = _build_init_data(user_id=user_id)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # player=[10,9]=19 (не натурал), dealer=[10,6]=16 -> stand доигрывает:
        # +"2" -> 18 (>=17, стоп). player(19) > dealer(18) -> "win", 2x.
        monkeypatch.setattr(
            casino_service, "_rng", _FixedDeckRng(["10♠", "9♣", "10♥", "6♦", "2♠"])
        )
        game_id = await _start_fixed_hand(client, init_data, BLACKJACK_ACTION_FRESH_BANK_CHAT_ID, [])

        resp = await client.post(
            f"/api/v1/games/blackjack/{game_id}/action",
            params={"chat_id": BLACKJACK_ACTION_FRESH_BANK_CHAT_ID},
            headers={"X-Telegram-Init-Data": init_data},
            json={"action": "stand"},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "settled"
    assert body["outcome"]["result"] == "win"
    # Честный выигрыш — 200 (100 * 2.0); банк стартовал с 0, пополнился
    # только ставкой (100) до выплаты — capped-payout == ставке.
    assert body["payout"] == 100
    assert body["payout"] < int(100 * casino_service.BLACKJACK_WIN_MULT)
    assert body["bank_capped"] is True


@pytest.mark.asyncio
async def test_blackjack_action_double_win_on_funded_bank_reports_bank_capped_false(monkeypatch):
    """Обратная сторона двух тестов выше: тот же класс выигрыша, но через
    `double` (эффективная ставка х2, см. bank_capped-комментарий
    post_blackjack_action в api/routes/games.py — `game_row.bet`/`result["bet"]`
    остаётся ОДИНАРНЫМ даже после double, роут обязан сам удвоить его для
    честной формулы) и на заведомо богатом банке — `bank_capped: false`,
    выплата равна честному итогу ПОЛНОСТЬЮ (bet * 2 удвоения * 2.0 множителя
    win). Без этой половины `bank_capped: true` было бы неотличимо от
    константы (тот же аргумент, что у `test_teto_slots_win_on_funded_bank_
    reports_bank_capped_false`)."""
    monkeypatch.setattr(telegram_client, "get_chat_member_status", AsyncMock(return_value="member"))
    user_id = 300515
    await _ensure_user(user_id)
    await _topup(BLACKJACK_FUNDED_BANK_CHAT_ID, user_id)
    init_data = _build_init_data(user_id=user_id)

    async with SessionLocal() as db_session:
        await economy_service.credit_bank(
            db_session, BLACKJACK_FUNDED_BANK_CHAT_ID, 1_000_000,
            kind="test_seed", ref_id=f"test_bj_funded_bank:{uuid.uuid4()}",
        )
        await db_session.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # player=[8,4]=12, dealer=[7,5]=12 (оба не натурал); double добирает
        # РОВНО одну карту "9" -> player=21, затем дилер доигрывает "5♥"
        # (12+5=17, стоп). player(21) > dealer(17) -> "win", 2x на удвоенной
        # ставке (bet_effective = bet*2).
        monkeypatch.setattr(
            casino_service, "_rng", _FixedDeckRng(["8♠", "4♠", "7♠", "5♠", "9♠", "5♥"])
        )
        game_id = await _start_fixed_hand(client, init_data, BLACKJACK_FUNDED_BANK_CHAT_ID, [])

        resp = await client.post(
            f"/api/v1/games/blackjack/{game_id}/action",
            params={"chat_id": BLACKJACK_FUNDED_BANK_CHAT_ID},
            headers={"X-Telegram-Init-Data": init_data},
            json={"action": "double"},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "settled"
    assert body["outcome"]["result"] == "win"
    assert body["bet"] == 100  # game_row.bet остаётся одинарным даже после double
    # Честный итог удвоенной раздачи — 400 (100 * 2 удвоения * 2.0 множителя
    # win); богатый банк платит его целиком, без капа.
    assert body["payout"] == 400
    assert body["bank_capped"] is False


# --- POST /api/v1/games/crash (start) + /crash/{id}/cashout ------------------
#
# Стейтфул real-time раунд (D-03) — та же форма, что блэкджек выше (game_id
# из start-ответа переиспользуется в cashout), но БЕЗ тела запроса у cashout
# вовсе (никакого "action"/"current multiplier" клиент прислать не может —
# сервер сам считает elapsed по `datetime.utcnow()`, см. модульный докстринг
# api/routes/games.py). `_ForcedCrashRng` форсирует точку краша так же, как
# `_FixedDeckRng` форсирует колоду блэкджека; `_backdate_crash_started_at`
# отодвигает `state.started_at` раунда напрямую в БД (тот же приём сетапа,
# что `_drain_bank`/`_force_settle_leftover_game` выше), чтобы тесты не
# зависели от реального времени выполнения самого теста.


async def _backdate_crash_started_at(game_id: int, seconds_ago: float) -> None:
    """Отодвигает `state["started_at"]` активного раунда Crash на
    `seconds_ago` секунд в прошлое напрямую через ORM — гарантирует, что
    следующий `crash_cash_out` увидит заведомо большой `elapsed`, без
    зависимости от того, сколько реального wall-clock времени тест-раннер
    фактически потратил между HTTP-вызовами start/cashout (сетап окружения,
    не денежная операция — та же дисциплина, что `_drain_bank` выше)."""
    new_started_at = (datetime.utcnow() - timedelta(seconds=seconds_ago)).isoformat()
    async with SessionLocal() as db_session:
        game_row = (
            await db_session.execute(select(CasinoGame).where(CasinoGame.id == game_id))
        ).scalar_one()
        state = dict(game_row.state)
        state["started_at"] = new_started_at
        game_row.state = state
        await db_session.commit()


@pytest.mark.asyncio
async def test_crash_start_valid_bet_returns_200_with_active_round(monkeypatch):
    monkeypatch.setattr(telegram_client, "get_chat_member_status", AsyncMock(return_value="member"))
    monkeypatch.setattr(casino_service, "_rng", _ForcedCrashRng(0.99))
    user_id = 300520
    await _ensure_user(user_id)
    await _topup(CRASH_CHAT_ID, user_id)
    init_data = _build_init_data(user_id=user_id)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/games/crash",
            params={"chat_id": CRASH_CHAT_ID},
            headers={"X-Telegram-Init-Data": init_data},
            json={"bet": 100, "idem_key": str(uuid.uuid4())},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "active"
    assert body["bet"] == 100
    assert "id" in body
    assert "started_at" in body
    # Раунд ещё активен — точка краша НИКОГДА не раскрывается заранее (иначе
    # клиент мог бы кэшаутиться за миллисекунду до неё без всякого риска).
    assert "crash_point" not in body
    assert "outcome" not in body
    assert "payout" not in body
    # bank_capped — честный False, не отсутствующий ключ (см. bank_capped-
    # комментарий post_crash_start в api/routes/games.py).
    assert body["bank_capped"] is False

    await _force_settle_leftover_game(body["id"])


@pytest.mark.asyncio
async def test_crash_start_missing_init_data_returns_401():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/games/crash",
            params={"chat_id": CRASH_CHAT_ID},
            json={"bet": 100, "idem_key": str(uuid.uuid4())},
        )

    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_crash_start_bet_below_minimum_returns_400(monkeypatch):
    monkeypatch.setattr(telegram_client, "get_chat_member_status", AsyncMock(return_value="member"))
    user_id = 300521
    await _ensure_user(user_id)
    await _topup(CRASH_CHAT_ID, user_id)
    init_data = _build_init_data(user_id=user_id)
    assert 1 < settings.casino_min_bet

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/games/crash",
            params={"chat_id": CRASH_CHAT_ID},
            headers={"X-Telegram-Init-Data": init_data},
            json={"bet": 1, "idem_key": str(uuid.uuid4())},
        )

    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_crash_start_ignores_foreign_user_id_in_body_idor(monkeypatch):
    """T-04.2-02: та же IDOR-защита, что и у остальных игр этого файла —
    `user_id` в теле запроса просто отсутствует в `CrashBet`, лишнее поле
    молча игнорируется FastAPI/Pydantic."""
    monkeypatch.setattr(telegram_client, "get_chat_member_status", AsyncMock(return_value="member"))
    monkeypatch.setattr(casino_service, "_rng", _ForcedCrashRng(0.99))
    attacker_id = 300522
    victim_id = 300523
    await _ensure_user(attacker_id)
    await _topup(CRASH_CHAT_ID, attacker_id)
    await _ensure_user(victim_id)
    await _topup(CRASH_CHAT_ID, victim_id)
    init_data = _build_init_data(user_id=attacker_id)
    idem_key = str(uuid.uuid4())

    victim_before = await _get_balance(CRASH_CHAT_ID, victim_id)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/games/crash",
            params={"chat_id": CRASH_CHAT_ID},
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

    victim_after = await _get_balance(CRASH_CHAT_ID, victim_id)
    assert victim_after == victim_before

    await _force_settle_leftover_game(game_row.id)


async def _start_crash_round(client, init_data: str, chat_id: int, forced_r: float, bet: int = 100):
    """Хелпер: стартует Crash-раунд через реальный HTTP start-роут с
    форсированной точкой краша, возвращает `game_id` из ответа.
    `casino_service._rng` ДОЛЖЕН быть замонкипатчен `_ForcedCrashRng(forced_r)`
    ДО вызова (та же форма, что `_start_fixed_hand` у блэкджека)."""
    resp = await client.post(
        "/api/v1/games/crash",
        params={"chat_id": chat_id},
        headers={"X-Telegram-Init-Data": init_data},
        json={"bet": bet, "idem_key": str(uuid.uuid4())},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "active"
    return body["id"]


@pytest.mark.asyncio
async def test_crash_cashout_full_round_trip_returns_200_with_sane_balances(monkeypatch):
    """Полный старт -> кэшаут раунд-трип на заведомо богатом банке: 200,
    `status=="settled"`, `outcome.result=="cashed_out"`, payout совпадает с
    "честной" формулой (`bet * outcome.multiplier`, Decimal), баланс игрока
    после кэшаута равен балансу после старта плюс payout — без капа (богатый
    банк). Точка краша форсирована высокой (98.00x), реальное elapsed-время
    между HTTP-вызовами start/cashout остаётся крошечным по сравнению с ней,
    так что раунд гарантированно застаёт кэшаут ДО краша."""
    monkeypatch.setattr(telegram_client, "get_chat_member_status", AsyncMock(return_value="member"))
    user_id = 300524
    await _ensure_user(user_id)
    await _topup(CRASH_FUNDED_BANK_CHAT_ID, user_id, min_balance=10_000)
    init_data = _build_init_data(user_id=user_id)

    async with SessionLocal() as db_session:
        await economy_service.credit_bank(
            db_session, CRASH_FUNDED_BANK_CHAT_ID, 1_000_000,
            kind="test_seed", ref_id=f"test_crash_funded_bank:{uuid.uuid4()}",
        )
        await db_session.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        monkeypatch.setattr(casino_service, "_rng", _ForcedCrashRng(0.99))
        game_id = await _start_crash_round(client, init_data, CRASH_FUNDED_BANK_CHAT_ID, 0.99)
        balance_after_start = await _get_balance(CRASH_FUNDED_BANK_CHAT_ID, user_id)

        resp = await client.post(
            f"/api/v1/games/crash/{game_id}/cashout",
            params={"chat_id": CRASH_FUNDED_BANK_CHAT_ID},
            headers={"X-Telegram-Init-Data": init_data},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "settled"
    assert body["id"] == game_id
    assert body["outcome"]["result"] == "cashed_out"
    assert body["cashed_out_multiplier"] == body["outcome"]["multiplier"]
    # Точка краша форсирована `forced_r=0.99` (~98.00x — не РОВНО 98.00:
    # `Decimal(0.99)` идёт через двоичное float-представление 0.99, не через
    # точную десятичную "0.99", см. `crash_engine.generate_crash_point`'s
    # докстринг про единственную float-точку входа), в любом случае никогда
    # не совпадёт со скромным live-множителем почти-мгновенного кэшаута.
    assert Decimal("90.00") < Decimal(body["crash_point"]) <= Decimal("100.00")
    assert Decimal(body["outcome"]["multiplier"]) < Decimal(body["crash_point"])

    # "Честная" выплата — та же Decimal-арифметика, что и route/casino_service
    # (bet * live_mult, floor через int()) от УЖЕ возвращённого множителя.
    fair_payout = int(Decimal(body["bet"]) * Decimal(body["outcome"]["multiplier"]))
    assert body["payout"] == fair_payout
    assert body["bank_capped"] is False

    balance_after_cashout = body["user_balance_after"]
    assert balance_after_cashout == balance_after_start + body["payout"]
    assert await _get_balance(CRASH_FUNDED_BANK_CHAT_ID, user_id) == balance_after_cashout


@pytest.mark.asyncio
async def test_crash_cashout_on_foreign_user_returns_404_idor(monkeypatch):
    """T-04.2-02 (crash): та же IDOR-проверка, что и
    `test_blackjack_action_on_foreign_game_returns_404_idor` — attacker не
    может кэшаутить game_id ЖЕРТВЫ, используя СВОЙ initData. SELECT в
    `crash_cash_out` фильтрует по user_id (среди прочего) -> чужой раунд
    структурно неотличим от несуществующего -> CasinoError -> 404. Раунд
    жертвы и её баланс должны остаться нетронутыми (раунд остаётся
    "active" — атака не смогла его даже settle'ить)."""
    monkeypatch.setattr(telegram_client, "get_chat_member_status", AsyncMock(return_value="member"))
    attacker_id = 300525
    victim_id = 300526
    await _ensure_user(attacker_id)
    await _topup(CRASH_CHAT_ID, attacker_id)
    await _ensure_user(victim_id)
    await _topup(CRASH_CHAT_ID, victim_id)
    attacker_init_data = _build_init_data(user_id=attacker_id)
    victim_init_data = _build_init_data(user_id=victim_id)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        monkeypatch.setattr(casino_service, "_rng", _ForcedCrashRng(0.99))
        victim_game_id = await _start_crash_round(client, victim_init_data, CRASH_CHAT_ID, 0.99)
        victim_balance_before = await _get_balance(CRASH_CHAT_ID, victim_id)

        resp = await client.post(
            f"/api/v1/games/crash/{victim_game_id}/cashout",
            params={"chat_id": CRASH_CHAT_ID},
            headers={"X-Telegram-Init-Data": attacker_init_data},
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

    victim_balance_after = await _get_balance(CRASH_CHAT_ID, victim_id)
    assert victim_balance_after == victim_balance_before

    await _force_settle_leftover_game(victim_game_id)


@pytest.mark.asyncio
async def test_crash_cashout_with_foreign_chat_id_returns_404(monkeypatch):
    """Тот же 4-way SELECT-фильтр `crash_cash_out` (`id`/`user_id`/`chat_id`/
    `game`), что и `test_blackjack_action_double_with_foreign_chat_id_returns_404`
    — раунд открыт в CRASH_CHAT_ID (чат A); тот же игрок пытается кэшаутить,
    подставив chat_id ДРУГОГО чата (B), где он тоже участник. Без chat_id в
    фильтре запрос из чата B "нашёл" бы раунд чата A по одному user_id —
    структурный IDOR даже без прямой денежной дыры (выплата всё равно ушла
    бы в реальный `game_row.chat_id`). Теперь такой раунд структурно
    неотличим от несуществующего -> 404, ни раунд, ни балансы обоих чатов не
    меняются."""
    monkeypatch.setattr(telegram_client, "get_chat_member_status", AsyncMock(return_value="member"))
    other_chat_id = -900556
    user_id = 300527
    await _ensure_user(user_id)
    await _topup(CRASH_CHAT_ID, user_id)
    await _topup(other_chat_id, user_id)
    init_data = _build_init_data(user_id=user_id)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        monkeypatch.setattr(casino_service, "_rng", _ForcedCrashRng(0.99))
        game_id = await _start_crash_round(client, init_data, CRASH_CHAT_ID, 0.99)
        balance_a_before = await _get_balance(CRASH_CHAT_ID, user_id)
        balance_b_before = await _get_balance(other_chat_id, user_id)

        resp = await client.post(
            f"/api/v1/games/crash/{game_id}/cashout",
            params={"chat_id": other_chat_id},
            headers={"X-Telegram-Init-Data": init_data},
        )

    assert resp.status_code == 404

    async with SessionLocal() as verify_session:
        game_row = (
            await verify_session.execute(select(CasinoGame).where(CasinoGame.id == game_id))
        ).scalar_one()
    assert game_row.status == "active"
    assert game_row.chat_id == CRASH_CHAT_ID

    assert await _get_balance(CRASH_CHAT_ID, user_id) == balance_a_before
    assert await _get_balance(other_chat_id, user_id) == balance_b_before

    await _force_settle_leftover_game(game_id)


@pytest.mark.asyncio
async def test_crash_cashout_missing_init_data_returns_401():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/games/crash/999999/cashout",
            params={"chat_id": CRASH_CHAT_ID},
        )

    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_crash_cashout_on_nonexistent_game_id_returns_404(monkeypatch):
    monkeypatch.setattr(telegram_client, "get_chat_member_status", AsyncMock(return_value="member"))
    user_id = 300528
    await _ensure_user(user_id)
    init_data = _build_init_data(user_id=user_id)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/games/crash/999999999/cashout",
            params={"chat_id": CRASH_CHAT_ID},
            headers={"X-Telegram-Init-Data": init_data},
        )

    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_crash_cashout_on_settled_round_replays_stored_result(monkeypatch):
    """T-04.1-09-форма (crash): повторный кэшаут на уже settled раунде —
    идемпотентный no-op, роут возвращает 200 с сохранённым исходом, а не
    ошибку. Деньги не двигаются повторно."""
    monkeypatch.setattr(telegram_client, "get_chat_member_status", AsyncMock(return_value="member"))
    user_id = 300529
    await _ensure_user(user_id)
    await _topup(CRASH_CHAT_ID, user_id)
    init_data = _build_init_data(user_id=user_id)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        monkeypatch.setattr(casino_service, "_rng", _ForcedCrashRng(0.99))
        game_id = await _start_crash_round(client, init_data, CRASH_CHAT_ID, 0.99)

        first = await client.post(
            f"/api/v1/games/crash/{game_id}/cashout",
            params={"chat_id": CRASH_CHAT_ID},
            headers={"X-Telegram-Init-Data": init_data},
        )
        assert first.status_code == 200
        assert first.json()["status"] == "settled"
        balance_after_settle = await _get_balance(CRASH_CHAT_ID, user_id)

        second = await client.post(
            f"/api/v1/games/crash/{game_id}/cashout",
            params={"chat_id": CRASH_CHAT_ID},
            headers={"X-Telegram-Init-Data": init_data},
        )

    assert second.status_code == 200
    body = second.json()
    assert body["status"] == "settled"
    assert body["outcome"] == first.json()["outcome"]
    assert body["payout"] == first.json()["payout"]
    assert body["bank_capped"] == first.json()["bank_capped"]

    balance_after_replay = await _get_balance(CRASH_CHAT_ID, user_id)
    assert balance_after_replay == balance_after_settle  # деньги не двинулись повторно


@pytest.mark.asyncio
async def test_crash_cashout_instant_bust_reports_payout_zero_and_bank_capped_false(monkeypatch):
    """Мгновенный краш (`forced_r=0.0` -> `crash_point==1.00x`, см.
    `crash_engine.generate_crash_point`): `multiplier_at(elapsed) >= 1.00`
    ВСЕГДА (растёт от 1.00 при `elapsed==0`), так что `live_mult <
    crash_point(1.00)` структурно НИКОГДА не может быть true — раунд
    гарантированно busted при любом elapsed, полностью детерминированно, без
    зависимости от реального времени выполнения теста (в отличие от
    "выигрышных" тестов выше, где точка краша высокая, а не низкая)."""
    monkeypatch.setattr(telegram_client, "get_chat_member_status", AsyncMock(return_value="member"))
    user_id = 300530
    await _ensure_user(user_id)
    await _topup(CRASH_CHAT_ID, user_id)
    init_data = _build_init_data(user_id=user_id)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        monkeypatch.setattr(casino_service, "_rng", _ForcedCrashRng(0.0))
        game_id = await _start_crash_round(client, init_data, CRASH_CHAT_ID, 0.0)
        balance_before_cashout = await _get_balance(CRASH_CHAT_ID, user_id)

        resp = await client.post(
            f"/api/v1/games/crash/{game_id}/cashout",
            params={"chat_id": CRASH_CHAT_ID},
            headers={"X-Telegram-Init-Data": init_data},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "settled"
    assert body["outcome"]["result"] == "busted"
    assert Decimal(body["crash_point"]) == Decimal("1.00")
    assert body["cashed_out_multiplier"] is None
    assert body["payout"] == 0
    # Честная выплата проигрыша тоже 0 (см. _crash_fair_payout) — bank_capped
    # обязан остаться False, а не стать True из-за "0 < 0".
    assert body["bank_capped"] is False

    balance_after_cashout = await _get_balance(CRASH_CHAT_ID, user_id)
    assert balance_after_cashout == balance_before_cashout  # проигрыш не платит ничего


@pytest.mark.asyncio
async def test_crash_cashout_win_on_empty_bank_reports_bank_capped(monkeypatch):
    """Регрессия по тому же классу D-06-инцидента, что и
    `test_blackjack_action_stand_win_on_empty_bank_reports_bank_capped`, для
    Crash. Свежий (нулевой) chat_bank пополняется РОВНО ставкой самой же
    ставкой (`_debit_stake`) до выплаты — точка краша форсирована высокой
    (98.00x), `started_at` отодвинут на 5с в прошлое напрямую в БД, чтобы
    live-множитель кэшаута гарантированно оказался заметно выше 1.00x (не
    зависит от реальной скорости выполнения самого теста) — честная выплата
    (bet * live_mult) не влезает в банк из одной лишь ставки, capped-payout
    урезается до размера самой ставки, банк возвращается к 0 (self-cleaning
    для повторных прогонов, та же дисциплина, что у blackjack-эквивалента)."""
    monkeypatch.setattr(telegram_client, "get_chat_member_status", AsyncMock(return_value="member"))
    user_id = 300531
    await _ensure_user(user_id)
    await _topup(CRASH_FRESH_BANK_CHAT_ID, user_id)
    init_data = _build_init_data(user_id=user_id)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        monkeypatch.setattr(casino_service, "_rng", _ForcedCrashRng(0.99))
        game_id = await _start_crash_round(client, init_data, CRASH_FRESH_BANK_CHAT_ID, 0.99)
        await _backdate_crash_started_at(game_id, seconds_ago=5.0)

        resp = await client.post(
            f"/api/v1/games/crash/{game_id}/cashout",
            params={"chat_id": CRASH_FRESH_BANK_CHAT_ID},
            headers={"X-Telegram-Init-Data": init_data},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "settled"
    assert body["outcome"]["result"] == "cashed_out"
    # Живой множитель после ~5с реального роста ГАРАНТИРОВАННО выше 1.00x —
    # честная выплата больше самой ставки, банк не может её покрыть целиком.
    assert Decimal(body["outcome"]["multiplier"]) > Decimal("1.00")
    fair_payout = int(Decimal(body["bet"]) * Decimal(body["outcome"]["multiplier"]))
    assert fair_payout > 100
    # Банк стартовал с 0, пополнился только ставкой (100) до выплаты —
    # capped-payout == ставке.
    assert body["payout"] == 100
    assert body["payout"] < fair_payout
    assert body["bank_capped"] is True


# --- GET /api/v1/games/crash/{id} (peek, read-only polling) ------------------
#
# Баг-репорт 2026-08-07 (Михаил): клиентский тикер не мог узнать, что раунд
# уже реально лопнул на сервере, и рисовал растущий множитель ещё долго
# ПОСЛЕ настоящего краша — casino_service.peek_crash закрывает эту дыру.
# Единственный роут этой секции, который НИКОГДА не платит и не settle'ит —
# тесты ниже отдельно проверяют именно это (баланс/статус в БД не меняются).


@pytest.mark.asyncio
async def test_crash_peek_get_returns_active_without_pending_bust_when_not_yet_crashed(monkeypatch):
    monkeypatch.setattr(telegram_client, "get_chat_member_status", AsyncMock(return_value="member"))
    user_id = 300532
    await _ensure_user(user_id)
    await _topup(CRASH_CHAT_ID, user_id)
    init_data = _build_init_data(user_id=user_id)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        monkeypatch.setattr(casino_service, "_rng", _ForcedCrashRng(0.99))  # высокая точка краша
        game_id = await _start_crash_round(client, init_data, CRASH_CHAT_ID, 0.99)

        resp = await client.get(
            f"/api/v1/games/crash/{game_id}",
            params={"chat_id": CRASH_CHAT_ID},
            headers={"X-Telegram-Init-Data": init_data},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "active"
    assert "pending_bust" not in body
    assert "crash_point" not in body
    assert "outcome" not in body
    # GET peek никогда не двигает деньги — в отличие от start/cashout, тут
    # нет ни balance_events, ни user_balance_after в ответе вовсе.
    assert "user_balance_after" not in body

    await _force_settle_leftover_game(game_id)


@pytest.mark.asyncio
async def test_crash_peek_get_returns_pending_bust_for_overdue_round_without_settling(monkeypatch):
    """Точка краша форсирована низкой (1.96x), `started_at` отодвинут далеко
    в прошлое — сервер по реальному времени уже давно бы посчитал раунд
    лопнувшим, но пока никто не вызвал cashout, БД-статус остаётся
    "active". Peek обязан честно сообщить `pending_bust: true`, но НЕ
    settle'ить и НЕ платить сам (это по-прежнему исключительно работа
    cashout)."""
    monkeypatch.setattr(telegram_client, "get_chat_member_status", AsyncMock(return_value="member"))
    user_id = 300533
    await _ensure_user(user_id)
    await _topup(CRASH_CHAT_ID, user_id)
    init_data = _build_init_data(user_id=user_id)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        monkeypatch.setattr(casino_service, "_rng", _ForcedCrashRng(0.5))  # crash_point 1.96
        game_id = await _start_crash_round(client, init_data, CRASH_CHAT_ID, 0.5)
        await _backdate_crash_started_at(game_id, seconds_ago=60.0)
        balance_before_peek = await _get_balance(CRASH_CHAT_ID, user_id)

        resp = await client.get(
            f"/api/v1/games/crash/{game_id}",
            params={"chat_id": CRASH_CHAT_ID},
            headers={"X-Telegram-Init-Data": init_data},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "active"  # БД не тронута — peek не settle'ит
    assert body["pending_bust"] is True
    assert "crash_point" not in body
    assert "outcome" not in body

    async with SessionLocal() as verify_session:
        game_row = (
            await verify_session.execute(select(CasinoGame).where(CasinoGame.id == game_id))
        ).scalar_one()
    assert game_row.status == "active"
    assert game_row.payout == 0
    assert game_row.outcome is None
    assert await _get_balance(CRASH_CHAT_ID, user_id) == balance_before_peek

    await _force_settle_leftover_game(game_id)


@pytest.mark.asyncio
async def test_crash_peek_get_missing_init_data_returns_401():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            "/api/v1/games/crash/999999",
            params={"chat_id": CRASH_CHAT_ID},
        )

    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_crash_peek_get_on_foreign_chat_returns_404_idor(monkeypatch):
    """Тот же 4-way SELECT-фильтр, что cashout — читай
    `test_crash_cashout_with_foreign_chat_id_returns_404` за полным разбором
    сценария атаки; peek обязан быть защищён тем же способом."""
    monkeypatch.setattr(telegram_client, "get_chat_member_status", AsyncMock(return_value="member"))
    user_id = 300534
    await _ensure_user(user_id)
    await _topup(CRASH_CHAT_ID, user_id)
    init_data = _build_init_data(user_id=user_id)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        monkeypatch.setattr(casino_service, "_rng", _ForcedCrashRng(0.99))
        game_id = await _start_crash_round(client, init_data, CRASH_CHAT_ID, 0.99)

        resp = await client.get(
            f"/api/v1/games/crash/{game_id}",
            params={"chat_id": CRASH_FUNDED_BANK_CHAT_ID},  # чужой чат
            headers={"X-Telegram-Init-Data": init_data},
        )

    assert resp.status_code == 404

    await _force_settle_leftover_game(game_id)


@pytest.mark.asyncio
async def test_crash_peek_get_on_nonexistent_game_id_returns_404(monkeypatch):
    monkeypatch.setattr(telegram_client, "get_chat_member_status", AsyncMock(return_value="member"))
    user_id = 300535
    await _ensure_user(user_id)
    init_data = _build_init_data(user_id=user_id)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            "/api/v1/games/crash/999999999",
            params={"chat_id": CRASH_CHAT_ID},
            headers={"X-Telegram-Init-Data": init_data},
        )

    assert resp.status_code == 404
