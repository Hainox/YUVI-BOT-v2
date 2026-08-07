"""Идемпотентное денежное ядро казино (04.1) — settle-раунд, поверх которого
построены coinflip/dice/roulette (слоты и блэкджек — 04.1-02/03, переиспользуют
`_settle`/`pay_from_bank` отсюда). Crash (04.1-XX) — стейтфул real-time раунд
той же формы, что блэкджек (тот же `CasinoGame`/`state` JSONB/переход
"active"->"settled" как гард идемпотентности), но крутится не клиентским
действием, а чистым прошедшим wall-clock временем — вся точка краша/
живой-множитель арифметика вынесена в отдельный чистый `crash_engine.py`
(см. его докстринг за RTP-инвариантом и Decimal-дисциплиной).

Деньги двигает ТОЛЬКО через `bot.services.economy_service` (`debit`+
`credit_bank` для ставки, `pay_from_bank` для выплаты) — этот модуль
НИКОГДА не пишет `user_balance`/`chat_bank`/`economy_tx` напрямую
(`economy_service.py` — единственный модуль с таким правом, см. его
докстринг).

Идемпотентность раунда — двухуровневая:
1. `CasinoGame(user_id, idem_key)` частичный UNIQUE (миграция 0005): повторный
   вызов с тем же `idem_key` возвращает СОХРАНЁННЫЙ исход из уже вставленной
   строки, деньги не двигаются повторно (`_settle` SELECT-проверяет ПЕРВЫМ).
2. `economy_service`-примитивы (`debit`/`credit_bank`/`pay_from_bank`) несут
   собственный `ref_id`-SAVEPOINT слой идемпотентности, производный от
   `idem_key` (`f"casino:{idem_key}"`, `...:bank`, `...:payout`) — backstop
   на случай гонки между SELECT-проверкой CasinoGame и вставкой (race-safe
   partial-UNIQUE + rollback, форма `markets_service.import_market`).

D-06 (payout capped to bank balance): выплата всегда идёт через
`economy_service.pay_from_bank`, который сам урезает сумму до остатка банка —
`chat_bank.balance` никогда не уходит в минус.

Все исходы — ТОЛЬКО через `secrets.SystemRandom()` (модульный RNG-seam
`_rng`, подменяется в тестах monkeypatch'ем) — server-authoritative,
клиент НИКОГДА не поставляет исход раунда (D-03/T-04.1-01).
"""

from __future__ import annotations

import logging
import secrets
import time
from datetime import datetime
from datetime import timedelta
from decimal import Decimal
from fractions import Fraction

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import settings
from bot.services import blackjack_engine
from bot.services import crash_engine
from bot.services import economy_service
from bot.services import jackpot_service
from bot.services import slot_engine
from bot.services import teto_slot_engine
from common.db.session import SessionLocal
from common.models.casino_game import CasinoGame

logger = logging.getLogger(__name__)

_rng = secrets.SystemRandom()

# --- Формулы игр (D-03) ------------------------------------------------------

COINFLIP_MULT = 1.98
DICE_HOUSE_EDGE = 0.02
# Точная рациональная форма DICE_HOUSE_EDGE (аудит-баг 2026-08-06): float
# 0.02 не представим точно в двоичной дроби, и `int(bet * (1 -
# DICE_HOUSE_EDGE) / win_prob)` копил ошибку округления через ДВА float-а
# (edge и win_prob) — на ~3.5% пар (target, bet) результат расходился с
# точным рациональным floor'ом (в обе стороны — не системная скидка в пользу
# банка, а именно шум). `_DICE_HOUSE_EDGE_EXACT` используется ТОЛЬКО в
# `dice_fair_payout` ниже; сам `DICE_HOUSE_EDGE` остаётся float —
# он всё ещё читается как informational-зеркало в miniapp (games/dice/
# +page.svelte, оценка мультипликатора ДО ставки, реальный payout всегда
# считает сервер).
_DICE_HOUSE_EDGE_EXACT = Fraction(2, 100)
ROULETTE_NUMBER_MULT = 36
ROULETTE_EVEN_MULT = 2
ROULETTE_DOZEN_MULT = 3

# Блэкджек (04.1-03, D-03/D-07/D-08)
BLACKJACK_TURN_SECONDS = 60
BLACKJACK_NATURAL_MULT = 2.5
BLACKJACK_WIN_MULT = 2.0
BLACKJACK_PUSH_MULT = 1.0

# Европейская рулетка (0-36): стандартный набор красных номеров, остальные
# (кроме 0) — чёрные. 0 не имеет цвета и проигрывает все "внешние" ставки.
_ROULETTE_RED_NUMBERS = frozenset(
    {1, 3, 5, 7, 9, 12, 14, 16, 18, 19, 21, 23, 25, 27, 30, 32, 34, 36}
)

_ROULETTE_BET_TYPES = frozenset({"number", "color", "parity", "half", "dozen"})


# --- Исключения --------------------------------------------------------------


class CasinoError(Exception):
    """Базовое исключение модуля казино."""


class InvalidBet(CasinoError):
    """Ставка нарушает лимиты (D-04/D-05) или входные параметры игры невалидны."""


class GameNotActive(CasinoError):
    """Раунд/игра не в активном состоянии (зарезервировано для стейтфул-игр 04.1-03 — блэкджек)."""


class DuplicateRound(CasinoError):
    """Раунд с этим idem_key уже обрабатывается конкурентным запросом (race, не обычный replay)."""


class RateLimited(CasinoError):
    """Слишком частые спины одного игрока (анти-абьюз авто-спина, см. `_check_slots_throttle`)."""


# --- Валидация ставки (D-04/D-05) --------------------------------------------


def _validate_bet(bet: int, balance: int) -> None:
    """D-04: единая минимальная ставка для всех игр казино и дуэлей.
    D-05: максимум — % от текущего баланса (по умолчанию 100%, т.е. фактически
    лимита сверх баланса нет — economy_service._guarded_debit добавляет
    структурный запрет ставить больше баланса)."""
    if bet < settings.casino_min_bet:
        raise InvalidBet(f"Минимальная ставка — {settings.casino_min_bet} ювиков")
    max_bet = int(balance * settings.casino_max_bet_pct)
    if bet > max_bet:
        raise InvalidBet(f"Максимальная ставка — {max_bet} ювиков (баланс {balance})")


# --- Стейк (списание ставки в банк) ------------------------------------------


async def _debit_stake(
    session: AsyncSession, chat_id: int, user_id: int, bet: int, idem_key: str
) -> bool:
    """Списывает ставку с игрока и зачисляет её в банк чата — общий "стейк"
    для всех игр, ref_id производный от idem_key. Тонкая обёртка над
    `economy_service.debit_to_bank` (IN-01 04.1-REVIEW: общий примитив с
    `duel_service._escrow_stake`). Возвращает False, если debit уже был
    применён ранее для этого idem_key (replay)."""
    return await economy_service.debit_to_bank(
        session, chat_id, user_id, bet, kind="casino_bet", ref_id=f"casino:{idem_key}"
    )


def _stored_result(game_row: CasinoGame) -> dict:
    return {
        "game": game_row.game,
        "bet": game_row.bet,
        "payout": game_row.payout,
        "outcome": game_row.outcome,
    }


async def _find_existing(session: AsyncSession, user_id: int, idem_key: str) -> CasinoGame | None:
    return (
        await session.execute(
            select(CasinoGame).where(CasinoGame.user_id == user_id, CasinoGame.idem_key == idem_key)
        )
    ).scalar_one_or_none()


# --- Идемпотентное стейтлес-settle-ядро --------------------------------------


async def _settle(
    session: AsyncSession,
    chat_id: int,
    user_id: int,
    game: str,
    bet: int,
    idem_key: str,
    compute,
) -> dict:
    """Идемпотентный раунд: replay по (user_id, idem_key) -> валидация ставки
    -> списание стейка -> RNG-исход (`compute()`) -> капнутая банком выплата
    -> запись `CasinoGame`. Коммитит один раз в конце (композирует
    некоммитящие примитивы `economy_service`)."""
    existing = await _find_existing(session, user_id, idem_key)
    if existing is not None:
        logger.info(
            "_settle: idem_key=%s (game=%s) уже обработан, возвращаем сохранённый исход",
            idem_key,
            game,
        )
        return _stored_result(existing)

    balance = await economy_service.get_balance(session, chat_id, user_id)
    _validate_bet(bet, balance)

    debited = await _debit_stake(session, chat_id, user_id, bet, idem_key)
    if not debited:
        # ref_id уже применялся в economy_service, но строки CasinoGame ещё
        # нет (гонка конкурентных запросов с одним idem_key) — не двигаем
        # деньги повторно; если строка уже появилась параллельным запросом,
        # отдаём её, иначе поднимаем DuplicateRound (ждём завершения гонки).
        existing = await _find_existing(session, user_id, idem_key)
        if existing is not None:
            return _stored_result(existing)
        raise DuplicateRound(f"Раунд уже обрабатывается конкурентным запросом (idem_key={idem_key})")

    payout, outcome = compute()

    paid = await economy_service.pay_from_bank(
        session, chat_id, user_id, payout, kind="casino_payout", ref_id=f"casino:{idem_key}:payout"
    )

    game_row = CasinoGame(
        chat_id=chat_id,
        user_id=user_id,
        game=game,
        bet=bet,
        payout=paid,
        outcome=outcome,
        status="settled",
        idem_key=idem_key,
    )
    try:
        session.add(game_row)
        await session.flush()
    except IntegrityError:
        # Партиал-UNIQUE (user_id, idem_key) — backstop против гонки между
        # SELECT-проверкой выше и flush здесь (форма markets_service.import_market).
        await session.rollback()
        existing = await _find_existing(session, user_id, idem_key)
        if existing is None:
            raise
        return _stored_result(existing)

    await session.commit()
    return {"game": game, "bet": bet, "payout": paid, "outcome": outcome}


# --- coinflip (D-03: 1.98x) ---------------------------------------------------


async def play_coinflip(
    session: AsyncSession, chat_id: int, user_id: int, bet: int, choice: str, idem_key: str
) -> dict:
    """Коинфлип 50/50: `choice` — 'heads'/'tails'. Выигрыш платит
    `int(bet * COINFLIP_MULT)`."""
    if choice not in ("heads", "tails"):
        raise InvalidBet("choice должен быть 'heads' или 'tails'")

    def compute() -> tuple[int, dict]:
        result = _rng.choice(["heads", "tails"])
        won = result == choice
        payout = int(bet * COINFLIP_MULT) if won else 0
        return payout, {"result": result, "won": won}

    return await _settle(session, chat_id, user_id, "coinflip", bet, idem_key, compute)


# --- dice (D-03: mult=(1-0.02)/win_prob) -------------------------------------


def dice_fair_payout(bet: int, target: int, direction: str) -> int:
    """Точная (без float-погрешности, см. `_DICE_HOUSE_EDGE_EXACT` выше)
    выплата на выигрыш кости — `floor(bet * (1 - edge) / win_prob)` в
    рациональной арифметике (`fractions.Fraction`), а не через float.
    Используется и здесь (`play_dice`), и `api/routes/games.py`'s
    `bank_capped` для dice — тот же паттерн, что `_ROULETTE_FAIR_MULT`/
    `_BLACKJACK_FAIR_MULT` там (пересчёт "честной" выплаты по данным,
    доступным вызывающему), но с ОБЩЕЙ функцией, а не задублированной
    формулой: два места с одной и той же формулой без общей функции — ровно
    как разошлись раньше (роут держал свою копию float-формулы,
    независимо ловящую ту же ошибку округления, что и здесь)."""
    win_prob = (
        Fraction(target - 1, 100) if direction == "under" else Fraction(100 - target, 100)
    )
    payout = bet * (1 - _DICE_HOUSE_EDGE_EXACT) / win_prob
    return payout.numerator // payout.denominator


async def play_dice(
    session: AsyncSession,
    chat_id: int,
    user_id: int,
    bet: int,
    target: int,
    direction: str,
    idem_key: str,
) -> dict:
    """Кости: `direction='under'` выигрывает при `roll < target`,
    `direction='over'` — при `roll > target`. `target` в диапазоне 2..99.
    Выплата на выигрыш — `dice_fair_payout(bet, target, direction)`."""
    if direction not in ("over", "under"):
        raise InvalidBet("direction должен быть 'over' или 'under'")
    if not (2 <= target <= 99):
        raise InvalidBet("target должен быть в диапазоне 2..99")

    def compute() -> tuple[int, dict]:
        roll = _rng.randint(1, 100)
        won = roll < target if direction == "under" else roll > target
        payout = dice_fair_payout(bet, target, direction) if won else 0
        return payout, {"roll": roll, "target": target, "direction": direction, "won": won}

    return await _settle(session, chat_id, user_id, "dice", bet, idem_key, compute)


# --- roulette (D-03: number 36x / color-parity-half 2x / dozen 3x) -----------


def _roulette_win(bet_type: str, bet_value, spin: int) -> tuple[bool, int]:
    """Возвращает (won, mult) для данного спина. 0 проигрывает все "внешние"
    ставки (color/parity/half/dozen) — не число."""
    if bet_type == "number":
        return spin == bet_value, ROULETTE_NUMBER_MULT
    if spin == 0:
        return False, 0
    if bet_type == "color":
        color = "red" if spin in _ROULETTE_RED_NUMBERS else "black"
        return color == bet_value, ROULETTE_EVEN_MULT
    if bet_type == "parity":
        parity = "even" if spin % 2 == 0 else "odd"
        return parity == bet_value, ROULETTE_EVEN_MULT
    if bet_type == "half":
        half = "low" if spin <= 18 else "high"
        return half == bet_value, ROULETTE_EVEN_MULT
    if bet_type == "dozen":
        dozen = 1 if spin <= 12 else (2 if spin <= 24 else 3)
        return dozen == bet_value, ROULETTE_DOZEN_MULT
    raise InvalidBet(f"Неизвестный тип ставки рулетки: {bet_type}")


def _validate_roulette_bet_value(bet_type: str, bet_value) -> None:
    """WR-06 (04.1-REVIEW): `bet_value` должен лежать в легальном домене для
    своего `bet_type`, иначе ставка молча всегда проигрывает (симптом сломанного
    клиента маскируется под обычный проигрыш вместо явной ошибки)."""
    if bet_type == "number":
        if not isinstance(bet_value, int) or isinstance(bet_value, bool) or not (0 <= bet_value <= 36):
            raise InvalidBet("bet_value для 'number' должен быть целым числом 0..36")
    elif bet_type == "color":
        if bet_value not in ("red", "black"):
            raise InvalidBet("bet_value для 'color' должен быть 'red' или 'black'")
    elif bet_type == "parity":
        if bet_value not in ("even", "odd"):
            raise InvalidBet("bet_value для 'parity' должен быть 'even' или 'odd'")
    elif bet_type == "half":
        if bet_value not in ("low", "high"):
            raise InvalidBet("bet_value для 'half' должен быть 'low' или 'high'")
    elif bet_type == "dozen":
        if bet_value not in (1, 2, 3):
            raise InvalidBet("bet_value для 'dozen' должен быть 1, 2 или 3")


async def play_roulette(
    session: AsyncSession,
    chat_id: int,
    user_id: int,
    bet: int,
    bet_type: str,
    bet_value,
    idem_key: str,
) -> dict:
    """Европейская рулетка (0-36). `bet_type` — 'number'/'color'/'parity'/
    'half'/'dozen', `bet_value` — конкретное число/'red'|'black'/'even'|'odd'/
    'low'|'high'/1|2|3 (дюжина) соответственно."""
    if bet_type not in _ROULETTE_BET_TYPES:
        raise InvalidBet(f"Неизвестный тип ставки рулетки: {bet_type}")
    _validate_roulette_bet_value(bet_type, bet_value)

    def compute() -> tuple[int, dict]:
        spin = _rng.randint(0, 36)
        won, mult = _roulette_win(bet_type, bet_value, spin)
        payout = int(bet * mult) if won else 0
        return payout, {"spin": spin, "bet_type": bet_type, "bet_value": bet_value, "won": won}

    return await _settle(session, chat_id, user_id, "roulette", bet, idem_key, compute)


# --- slots (D-05/D-06: Azumanga, RTP ~92.78%) --------------------------------

# Анти-абьюз авто-спина (миниапп крутит слоты в цикле без ручного тапа на
# каждый раунд): последний момент времени спина по user_id, in-memory —
# защитный троттлинг, не бизнес-данные, потеря при рестарте процесса не
# страшна (просто разрешит один спин чуть раньше срока). Ключ — user_id, не
# (chat_id, user_id): один и тот же игрок, спамящий из нескольких чатов
# параллельно, — тот же паттерн злоупотребления, который стоит гасить.
_last_slots_spin_at: dict[int, float] = {}


def _check_slots_throttle(user_id: int) -> None:
    """Отклоняет спин, если предыдущий спин ЭТОГО игрока был меньше
    `settings.casino_spin_min_interval_ms` назад. Только для НОВЫХ раундов —
    вызывающий (`play_slots`) обязан пропустить этот вызов, если это replay
    уже settled-раунда по тому же `idem_key`: иначе легитимный повторный
    вызов с тем же idem_key (обычная идемпотентность, напр.
    `test_play_slots_settles_and_is_idempotent`) ошибочно ловил бы
    RateLimited вместо честного replay-ответа. Ключ (dict) и сама функция
    общие для ВСЕХ слот-игр казино (Azumanga `play_slots` и Тето
    `play_teto_slots`) — это один и тот же анти-абьюз-концерн (авто-спин
    конкретного игрока) независимо от того, в какой слот он крутит, поэтому
    троттлинг-дикт не форкается по игре."""
    now = time.monotonic()
    last = _last_slots_spin_at.get(user_id)
    _last_slots_spin_at[user_id] = now
    if last is not None and (now - last) * 1000 < settings.casino_spin_min_interval_ms:
        raise RateLimited("Слишком часто — подождите немного между спинами")


async def play_slots(
    session: AsyncSession, chat_id: int, user_id: int, bet: int, idem_key: str
) -> dict:
    """Слот "Azumanga" (04.1-02) — server-authoritative спин по всем
    `slot_engine.TOTAL_LINES` (10) paylines одновременно. `bet` — суммарная
    ставка на раунд; должна быть положительным кратным `TOTAL_LINES`, откуда
    выводится `bet_per_line = bet // TOTAL_LINES` (ставка на каждую линию
    одинаковая, выбор подмножества линий не поддерживается — упрощение
    относительно черновика `webapp/slot-data.jsx`).

    RNG-исход (сетка + авто-доигранные фриспины) считает чистый
    `slot_engine.spin_grid`/`evaluate_grid`, вызванный ЧЕРЕЗ общий seam
    `_rng` этого модуля (НЕ через собственный `slot_engine._rng` — тот
    используется только для внутреннего авто-розыгрыша фриспинов). Деньги
    двигает исключительно `_settle` (стейк -> RNG -> капнутая банком выплата
    -> `CasinoGame`), здесь — только сборка `compute()`-замыкания.

    Джекпот (CASINO-06, jackpot_service) — ОТДЕЛЬНЫЙ слой поверх уже
    расчитанного settle, намеренно не встроенный в `_settle`/`compute()`:
    `_settle` — общее ядро всех игр казино, трогать его ради одной слот-
    фичи не нужно (та же дисциплина, что докстринг `_settle` про
    `bank_capped` в api/routes/games.py). Проверяем ДО `_settle`, был ли
    этот idem_key уже обработан (replay) — джекпот пополняется/бросает
    кубик ТОЛЬКО для подтверждённо нового спина, иначе повторный запрос
    вырастил бы пул или бросил кубик дважды за один и тот же спин. Пул
    пополняется/списывается ОТДЕЛЬНЫМ commit'ом после `_settle`'s
    собственного — крайне узкое окно (падение процесса между двумя
    commit'ами потеряло бы вклад этого спина в пул), приемлемо для
    бонусного слоя поверх уже надёжно закоммиченной основной выплаты.

    Перед этим проверяет `_check_slots_throttle` (`RateLimited`, если
    слишком рано) — единственная игра казино с клиентским авто-повтором
    ставок (авто-спин в miniapp), поэтому единственная с троттлингом частоты.
    Троттлинг НЕ применяется, если `idem_key` уже settled (`_find_existing`
    находит существующую строку) — это replay, а не новая ставка, `_settle`
    ниже вернёт сохранённый исход без повторного движения денег. `is_new_spin`
    вычисляется ЗДЕСЬ, до валидации ставки — тот же самый флаг переиспользует
    и джекпот-слой ниже, вторым запросом в БД его не дублируем."""
    is_new_spin = await _find_existing(session, user_id, idem_key) is None
    if is_new_spin:
        _check_slots_throttle(user_id)

    if bet <= 0 or bet % slot_engine.TOTAL_LINES != 0:
        raise InvalidBet(
            f"Ставка должна быть положительным кратным {slot_engine.TOTAL_LINES} "
            "(по числу линий слота)"
        )
    bet_per_line = bet // slot_engine.TOTAL_LINES

    def compute() -> tuple[int, dict]:
        grid = slot_engine.spin_grid(_rng)
        result = slot_engine.evaluate_grid(grid, bet_per_line)
        return result.total_payout, {
            "grid": result.grid,
            "wins": result.line_wins,
            "freespins": result.freespins,
            "scatter": result.scatter_count,
            "retrigger_awards": result.retrigger_awards,
            "freespin_rounds": result.freespin_rounds,
        }

    result = await _settle(session, chat_id, user_id, "slots", bet, idem_key, compute)

    jackpot = None
    if is_new_spin:
        jackpot = await jackpot_service.contribute_and_maybe_award(
            session, chat_id, user_id, bet, idem_key, _rng
        )
        await session.commit()

    return {**result, "jackpot": jackpot}


# --- teto_slots (Тето Брейнрот: Дрель-Хант — 04.1-XX, money-интеграция) ------


async def play_teto_slots(
    session: AsyncSession,
    chat_id: int,
    user_id: int,
    bet: int,
    idem_key: str,
    *,
    animation_sink: dict | None = None,
) -> dict:
    """Слот "Тето Брейнрот: Дрель-Хант" — money/DB-обвязка ПОВЕРХ уже готового
    и полностью протестированного чистого движка `teto_slot_engine.play_one_spin`
    (мегаблоки + тумбл-каскад + Дрель-Хант + лестница множителя, см. его
    докстринг). Построена буквально "по образцу `play_slots`" (см. докстринг
    `teto_slot_engine.py` — это прямо сформулированный там следующий шаг):
    те же `is_new_spin`/`_check_slots_throttle`/`bet % TOTAL_LINES`/
    `bet_per_line`/`compute()`-замыкание/`_settle`, тот же общий RNG-seam
    `_rng` (server-authoritative, D-03/T-04.1-01), тот же общий троттлинг-дикт
    `_last_slots_spin_at` (см. обновлённый докстринг `_check_slots_throttle`
    выше — тот же анти-абьюз-концерн авто-спина, что и у Azumanga, ключ по
    `user_id`, не форкается на второй dict под конкретную игру).

    Что ГЕНУИННО отличается от `play_slots`:
      - `TOTAL_LINES`/`bet_per_line` берутся из `teto_slot_engine` (50 линий —
        полный набор оригинала, см. `teto_tumble.TETO_PAYLINES`), а не из
        `slot_engine`.
      - `compute()` вызывает `teto_slot_engine.play_one_spin(_rng,
        bet_per_line)`, а не `slot_engine.spin_grid`/`evaluate_grid`, и
        ОБЯЗАН прогнать сырой результат через
        `teto_slot_engine.serialize_spin_result` ПЕРЕД тем, как отдать его
        `_settle` как `outcome` — сырой `play_one_spin`-dict содержит
        dataclass-инстансы (`MegaBlock`/`LadderState`), которые `CasinoGame.
        outcome` (JSONB) не проглотит как есть (см. докстринг
        `serialize_spin_result` — там же разбор конкретного риска: спин с
        фриспинами, обычный игровой путь, уронил бы commit ошибкой
        сериализации без этого шага).

    Намеренно БЕЗ джекпот-слоя (в отличие от `play_slots`, где после
    `_settle` идёт `jackpot_service.contribute_and_maybe_award`): прогрессивный
    джекпот (CASINO-06) по дизайну — фича конкретно слота Azumanga, а не
    общий слой поверх ЛЮБОГО слота казино; включать в его пул спины Тето —
    отдельное продуктовое решение, явно вне рамок этой задачи (см. roadmap.md),
    поэтому `jackpot_service` здесь не импортируется и не вызывается вовсе.

    `animation_sink` (keyword-only, по умолчанию `None`) — OUT-ПАРАМЕТР для
    покадрового сценария анимации (`teto_slot_engine.serialize_animation`).
    Если передан (любой dict, обычно пустой), он заполняется ЧЕРЕЗ `.update()`
    (владелец dict'а — вызывающий, мы его никогда не ребиндим). Если НЕ передан —
    `trace=None`, и поведение со стоимостью побайтово те же, что были до
    появления анимации: ни один существующий вызывающий (например будущая
    бот-команда `/teto`) ничего не платит за фичу, которой не пользуется.

    ПОЧЕМУ OUT-ПАРАМЕТР, А НЕ НОВЫЙ КЛЮЧ В ВОЗВРАЩАЕМОМ dict'е. Возвращаемое
    значение обязано остаться РОВНО `{"game","bet","payout","outcome"}`:
    `tests/test_casino_service.py::
    test_teto_slots_replay_of_settled_idem_key_not_throttled` проверяет
    `second == first` для двух вызовов с ОДНИМ `idem_key`, а ключ `animation`
    был бы dict'ом на свежем спине и `None` на replay — то есть добавление его
    в возврат сломало бы именно то свойство (идентичность ответа при повторе),
    которое эта игра обязана держать. Out-параметр кладёт "существует только
    для свежего спина" в побочный канал, где это и должно жить.

    ПОЧЕМУ `serialize_animation` ВЫЗЫВАЕТСЯ ПОСЛЕ `_settle`, А НЕ ВНУТРИ
    `compute()`. Две независимые причины, обе появились вместе с денежной
    тройкой конверта:
      1. Она физически не может быть заполнена внутри `compute()`: капнутая
         банком выплата (`economy_service.pay_from_bank`, D-06) становится
         известна ТОЛЬКО после `_settle` (`result["payout"]`), а без неё конверт
         несёт лишь неурезанный движковый итог — ровно тот дефект, из-за
         которого экран досчитывал бы счётчик до суммы, которой игрок не
         получил (разбор — в докстринге `serialize_animation`). Поэтому
         `paid_total` — обязательный аргумент, а не опция: конверт нельзя
         собрать, не назвав фактическую выплату.
      2. `compute()` выполняется ВНУТРИ единственной транзакции `_settle` —
         той, что держит row-lock на `chat_bank` (взят `pay_from_bank`).
         Сериализация трейса — до 120 op'ов x 36 блоков = ~4.3 тыс.
         `dataclasses.asdict`, сотни килобайт мусора — чистая
         презентационная работа, которой нечего делать под локом горячей
         строки чата. Внутри транзакции остался только сам прокрут (`compute`
         набивает СЫРОЙ трейс в `computed["trace"]` — список ссылок на уже
         существующие объекты, стоимость близка к нулю).
    Идемпотентность при этом не ослаблена: сериализуем ровно тогда же, когда
    и раньше заполняли sink — на подтверждённо СВЕЖЕМ спине, по той же сверке
    идентичности объекта `outcome` (см. ниже). Плата за перенос — если
    сериализация упадёт, она упадёт уже ПОСЛЕ commit'а раунда: игрок получит
    500 на спине, который на самом деле состоялся. Приемлемо, потому что (а)
    `serialize_animation` — чистая функция над уже провалидированными
    JSON-совместимыми данными (реалистичный отказ — только OOM), и (б) ретрай
    того же `idem_key` вернёт сохранённый исход с `animation: null` — штатный,
    уже задокументированный путь деградации, а не потерянные деньги.

    ПОЧЕМУ ТРЕЙС НЕ КЛАДЁТСЯ В `outcome` (и, следовательно, не переживает
    запрос): подробный разбор — в докстринге `serialize_animation`, коротко —
    +160% к размеру КАЖДОЙ строки `CasinoGame` данными без аудиторской
    ценности, а патологический спин добавил бы ~21 МБ JSON в тот самый INSERT,
    который выполняется ВНУТРИ транзакции `_settle`, держащей row-lock на
    `chat_bank` (`economy_service.pay_from_bank`) — удлинение блокировки
    горячей строки чата, а не просто потраченные байты. `_settle` при этом не
    трогается вовсе (его же правило: не менять общее ядро казино ради
    UI-фичи одной игры, та же дисциплина, что с `bank_capped`).

    КОНТРАКТ REPLAY: на идемпотентном повторе уже рассчитанного `idem_key`
    `_settle` возвращает сохранённый исход, НЕ вызывая `compute()` вовсе —
    значит трейса нет и `animation_sink` остаётся ПУСТЫМ (роут превратит это в
    `animation: null`). Синтезировать анимацию на replay нельзя
    принципиально: повторный `play_one_spin` съел бы свежий `_rng` и дал бы
    ДРУГОЙ спин, чем сохранённый `outcome` — игрок увидел бы анимацию спина,
    которого не было, с выигрышем, не совпадающим с изменением его баланса.
    Для денежного UI это худший возможный отказ; пустой sink — честный ответ.
    Тот же случай — узкая гонка, в которой `compute()` УЖЕ отработал, но
    `_settle` поймал `IntegrityError` на flush'е и вернул строку, записанную
    конкурентным запросом: тогда посчитанный нами спин выброшен, и sink обязан
    быть очищен, иначе наружу уехала бы анимация выброшенного спина при
    `outcome` от другого. Ловим это сверкой ИДЕНТИЧНОСТИ объекта `outcome`
    (см. ниже) — `_settle` отдаёт на свежем пути ровно тот объект, который
    вернул наш `compute()`, и любой другой объект означает "раунд посчитан не
    в этом запросе"."""
    is_new_spin = await _find_existing(session, user_id, idem_key) is None
    if is_new_spin:
        _check_slots_throttle(user_id)

    if bet <= 0 or bet % teto_slot_engine.TOTAL_LINES != 0:
        raise InvalidBet(
            f"Ставка должна быть положительным кратным {teto_slot_engine.TOTAL_LINES} "
            "(по числу линий слота)"
        )
    bet_per_line = bet // teto_slot_engine.TOTAL_LINES

    computed: dict = {}

    def compute() -> tuple[int, dict]:
        trace: list | None = [] if animation_sink is not None else None
        result = teto_slot_engine.play_one_spin(_rng, bet_per_line, trace=trace)
        outcome = teto_slot_engine.serialize_spin_result(result)
        computed["outcome"] = outcome
        computed["trace"] = trace
        return result["total_payout"], outcome

    result = await _settle(session, chat_id, user_id, "teto_slots", bet, idem_key, compute)

    if animation_sink is not None:
        # `"outcome" in computed` ПЕРВЫМ, а не `computed.get("outcome")`:
        # `.get` вернул бы `None` и на "compute не звался", и на "исход
        # сохранён с NULL-outcome", а `None is None` — истина, т.е. replay
        # такой строки полез бы за несуществующим `computed["trace"]`. У Тето
        # NULL-outcome сегодня недостижим, но сверка "наш ли это раунд" не
        # должна зависеть от значения поля — только от факта вызова compute.
        if "outcome" in computed and result["outcome"] is computed["outcome"]:
            animation_sink.update(
                teto_slot_engine.serialize_animation(
                    computed["trace"], paid_total=result["payout"]
                )
            )
        else:
            # Раунд пришёл НЕ из нашего `compute()` (обычный replay — там compute
            # даже не звался, либо гонка с IntegrityError-откатом внутри `_settle`).
            # Анимация обязана относиться к тому же спину, что `outcome`, поэтому
            # чистим sink на месте (`.clear()`, не ребиндим — dict чужой).
            animation_sink.clear()

    return result


# --- blackjack (D-03/D-07/D-08: стейтфул-раздача, деck/руки в state JSONB) ---


def _blackjack_view(game_row: CasinoGame) -> dict:
    """Публичный вид раздачи: пока активна — карты игрока и ТОЛЬКО верхняя
    (видимая) карта дилера (T-04.1-08 — вторая карта дилера скрыта до
    settle); после settle — обе карты дилера + итог/выплата."""
    state = game_row.state or {}
    view: dict = {
        "id": game_row.id,
        "status": game_row.status,
        "bet": game_row.bet,
        "player": state.get("player"),
    }
    if game_row.status == "active":
        dealer = state.get("dealer") or []
        view["dealer_upcard"] = dealer[0] if dealer else None
    else:
        view["dealer"] = state.get("dealer")
        view["payout"] = game_row.payout
        view["outcome"] = game_row.outcome
    return view


def _blackjack_deadline() -> str:
    return (datetime.utcnow() + timedelta(seconds=BLACKJACK_TURN_SECONDS)).isoformat()


async def _finalize_blackjack(
    session: AsyncSession,
    game_row: CasinoGame,
    player: list[str],
    dealer: list[str],
    outcome_name: str,
    payout: int,
    *,
    state: dict | None = None,
) -> None:
    """Общий финал раздачи: капнутая банком выплата (D-06, если payout > 0),
    запись outcome/payout, статус-переход "active"->"settled" (T-04.1-09).
    Не коммитит — коммитит вызывающий."""
    paid = 0
    if payout > 0:
        paid = await economy_service.pay_from_bank(
            session,
            game_row.chat_id,
            game_row.user_id,
            payout,
            kind="casino_payout",
            ref_id=f"casino:bj:{game_row.id}",
        )
    if state is not None:
        game_row.state = state
    game_row.outcome = {"result": outcome_name, "player": player, "dealer": dealer}
    game_row.payout = paid
    game_row.status = "settled"


async def start_blackjack(
    session: AsyncSession, chat_id: int, user_id: int, bet: int, idem_key: str
) -> dict:
    """Раздаёт руку блэкджека (04.1-03): списывает ставку в банк (общий
    `_debit_stake`), тасует колоду через общий сеанс `_rng`, кладёт 2 карты
    игроку + 2 дилеру (T-04.1-08 — колода server-authoritative, живёт в
    `CasinoGame.state` JSONB). Натурал игрока (2 карты, 21) settle'ится
    немедленно (натурал 2.5x или push при натурале у дилера, D-03) —
    иначе раздача остаётся `status="active"` до `blackjack_action`.

    Идемпотентна по `idem_key` — повторный вызов возвращает сохранённую
    раздачу без повторного движения денег (та же двухуровневая идемпотентность,
    что у `_settle`)."""
    existing = await _find_existing(session, user_id, idem_key)
    if existing is not None:
        logger.info(
            "start_blackjack: idem_key=%s уже обработан, возвращаем сохранённую раздачу",
            idem_key,
        )
        return _blackjack_view(existing)

    balance = await economy_service.get_balance(session, chat_id, user_id)
    _validate_bet(bet, balance)

    debited = await _debit_stake(session, chat_id, user_id, bet, idem_key)
    if not debited:
        existing = await _find_existing(session, user_id, idem_key)
        if existing is not None:
            return _blackjack_view(existing)
        raise DuplicateRound(f"Раздача уже обрабатывается конкурентным запросом (idem_key={idem_key})")

    deck = blackjack_engine.new_shuffled_deck(_rng)
    player_cards = [deck.pop(), deck.pop()]
    dealer_cards = [deck.pop(), deck.pop()]

    state = {
        "deck": deck,
        "player": player_cards,
        "dealer": dealer_cards,
        "bet": bet,
        "turn_deadline": _blackjack_deadline(),
    }

    game_row = CasinoGame(
        chat_id=chat_id,
        user_id=user_id,
        game="blackjack",
        bet=bet,
        payout=0,
        outcome=None,
        state=state,
        status="active",
        idem_key=idem_key,
    )
    try:
        session.add(game_row)
        await session.flush()  # нужен game_row.id для ref_id немедленного settle натурала ниже
    except IntegrityError:
        # Партиал-UNIQUE (user_id, idem_key) — backstop против гонки между
        # SELECT-проверкой выше и flush здесь (форма _settle/import_market).
        await session.rollback()
        existing = await _find_existing(session, user_id, idem_key)
        if existing is None:
            raise
        return _blackjack_view(existing)

    if blackjack_engine.is_natural(player_cards):
        outcome_name, mult = blackjack_engine.settle_outcome(player_cards, dealer_cards, True)
        payout = int(bet * mult)
        await _finalize_blackjack(session, game_row, player_cards, dealer_cards, outcome_name, payout)

    await session.commit()
    return _blackjack_view(game_row)


async def blackjack_action(
    session: AsyncSession, chat_id: int, game_id: int, user_id: int, action: str
) -> dict:
    """Действие в активной раздаче: `hit`/`stand`/`double`. `SELECT ... FOR
    UPDATE` на строку раздачи первым (контракт порядка блокировок), затем
    статус-переход "active"->"settled" САМ служит гардом идемпотентности
    (T-04.1-09, форма `markets_service.resolve_market`) — повторный вызов
    на уже settled-раздаче возвращает сохранённый исход, деньги не двигаются
    повторно. SELECT фильтрует и по `chat_id` (не только `user_id`/`game_id`):
    `double` списывает вторую ставку через `_debit_stake(session, chat_id,
    ...)`, а без этого фильтра игрок с активной раздачей в чате A мог
    подставить `chat_id` чата B (где он тоже участник) и оплатить удвоение
    из чужого банка, пока выплата всё равно уходила в `game_row.chat_id`
    (чат A) — банк A рос за чужой счёт.

    `hit` — добор одной карты; перебор (>21) settle'ится сразу как bust,
    иначе `state.turn_deadline` продлевается ещё на `BLACKJACK_TURN_SECONDS`
    (раздача остаётся активной). `stand` — дилер доигрывает (soft-17 stand,
    D-03) и раздача settle'ится. `double` — требует РОВНО двухкарточную
    открывающую раздачу; списывает ещё одну ставку (`_debit_stake`,
    `idem_key=f"{game_id}:double"`), добирает ровно одну карту, затем дилер
    доигрывает и раздача settle'ится на удвоенной ставке."""
    if action not in ("hit", "stand", "double"):
        raise InvalidBet("action должен быть 'hit'/'stand'/'double'")

    game_row = (
        await session.execute(
            select(CasinoGame)
            .where(
                CasinoGame.id == game_id,
                CasinoGame.user_id == user_id,
                CasinoGame.chat_id == chat_id,
                CasinoGame.game == "blackjack",
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if game_row is None:
        raise CasinoError(f"Раздача блэкджека #{game_id} не найдена")

    if game_row.status != "active":
        # Гард идемпотентности (T-04.1-09): повторный action на уже settled
        # раздаче — no-op, возвращаем сохранённый исход.
        await session.commit()
        return _blackjack_view(game_row)

    state = dict(game_row.state)
    deck = list(state["deck"])
    player = list(state["player"])
    dealer = list(state["dealer"])
    bet = state["bet"]

    if action == "hit":
        player.append(deck.pop())
        state["deck"] = deck
        state["player"] = player
        value, _ = blackjack_engine.hand_value(player)
        if value > 21:
            await _finalize_blackjack(session, game_row, player, dealer, "bust", 0, state=state)
        else:
            state["turn_deadline"] = _blackjack_deadline()
            game_row.state = state
        await session.commit()
        return _blackjack_view(game_row)

    if action == "stand":
        deck, dealer = blackjack_engine.dealer_play(deck, dealer)
        state["deck"] = deck
        state["dealer"] = dealer
        outcome_name, mult = blackjack_engine.settle_outcome(player, dealer, False)
        payout = int(bet * mult)
        await _finalize_blackjack(session, game_row, player, dealer, outcome_name, payout, state=state)
        await session.commit()
        return _blackjack_view(game_row)

    # action == "double"
    if len(player) != 2:
        raise InvalidBet("Удвоить можно только на исходной раздаче (2 карты)")

    debited = await _debit_stake(session, chat_id, user_id, bet, f"{game_id}:double")
    if not debited:
        # Гонка/повтор на double уже применённом ref_id: FOR UPDATE выше
        # обычно исключает это (см. WR-02 04.1-REVIEW), проверка — defense
        # in depth, не полагается на то, что блокировка никогда не сломается.
        raise CasinoError(f"Удвоение уже обработано (game_id={game_id})")

    bet_effective = bet * 2
    player.append(deck.pop())
    state["deck"] = deck
    state["player"] = player
    value, _ = blackjack_engine.hand_value(player)
    if value > 21:
        outcome_name, mult = "bust", 0.0
    else:
        deck, dealer = blackjack_engine.dealer_play(deck, dealer)
        state["deck"] = deck
        state["dealer"] = dealer
        outcome_name, mult = blackjack_engine.settle_outcome(player, dealer, False)
    payout = int(bet_effective * mult)
    await _finalize_blackjack(session, game_row, player, dealer, outcome_name, payout, state=state)
    await session.commit()
    return _blackjack_view(game_row)


# --- Таймаут-резолвер блэкджека (D-07/D-08, T-04.1-10) + APScheduler --------


async def resolve_blackjack_timeouts(session: AsyncSession) -> int:
    """Сканирует активные раздачи блэкджека с истёкшим `state.turn_deadline`
    (D-07: 60с на ход) и авто-стендит их (D-08) — дилер доигрывает по
    обычным правилам (soft-17 stand), исход settle'ится так, как будто
    игрок сам нажал "stand". Ставка НИКОГДА не замораживается навсегда
    (T-04.1-10).

    Per-row try/except (форма `markets_service.auto_resolve_external`) —
    одна застрявшая раздача не блокирует весь батч. Батч-wide `FOR UPDATE`
    здесь НЕ используется (WR-03 04.1-REVIEW): первичный SELECT только
    собирает id-кандидатов БЕЗ блокировки, каждая строка запрашивается и
    блокируется (`FOR UPDATE`) заново непосредственно перед финализацией,
    внутри своей собственной транзакции/commit — иначе commit первой же
    обработанной строки снял бы лок со ВСЕХ строк батча разом, открывая окно
    гонки с параллельным `blackjack_action` для ещё не обработанных строк.
    Статус перепроверяется ("active") после повторного лока — раздача могла
    уже settle'иться живым действием игрока между первичным SELECT и этим
    моментом. Возвращает число авто-стендённых раздач."""
    ids = (
        await session.execute(
            select(CasinoGame.id).where(
                CasinoGame.game == "blackjack",
                CasinoGame.status == "active",
                text("(state->>'turn_deadline')::timestamp <= now()"),
            )
        )
    ).scalars().all()

    resolved_count = 0
    for game_id in ids:
        try:
            game_row = (
                await session.execute(
                    select(CasinoGame).where(CasinoGame.id == game_id).with_for_update()
                )
            ).scalar_one_or_none()
            if game_row is None or game_row.status != "active":
                # Уже settle'илась (live-действие игрока) между первичным
                # SELECT и этим локом — не наш случай, no-op.
                await session.commit()
                continue

            state = dict(game_row.state)
            deck = list(state["deck"])
            player = list(state["player"])
            dealer = list(state["dealer"])
            bet = state["bet"]

            deck, dealer = blackjack_engine.dealer_play(deck, dealer)
            state["deck"] = deck
            state["dealer"] = dealer
            outcome_name, mult = blackjack_engine.settle_outcome(player, dealer, False)
            payout = int(bet * mult)

            await _finalize_blackjack(session, game_row, player, dealer, outcome_name, payout, state=state)
            await session.commit()
            resolved_count += 1
        except Exception:  # noqa: BLE001 - один застрявший раунд не должен ронять весь тик
            logger.exception(
                "resolve_blackjack_timeouts: не удалось авто-стендить game_id=%s", game_id
            )
            await session.rollback()

    return resolved_count


_BLACKJACK_TIMEOUTS_JOB_ID = "blackjack_timeouts"


def register_blackjack_timeouts(scheduler: AsyncIOScheduler) -> None:
    """Регистрирует авто-стенд просроченных раздач блэкджека как interval-job
    (30с — раздачи короткоживущие, 60-секундный таймаут требует частого
    скана), по образцу `markets_service.register_auto_close`: своя сессия,
    broad-except — тик обязан пережить любую ошибку и не уронить планировщик."""

    async def _job() -> None:
        async with SessionLocal() as session:
            try:
                resolved_count = await resolve_blackjack_timeouts(session)
                if resolved_count:
                    logger.info(
                        "blackjack_timeouts: авто-стенд просроченных раздач — %s", resolved_count
                    )
            except Exception:  # noqa: BLE001 - job обязан пережить любую ошибку и не уронить планировщик
                logger.exception("blackjack_timeouts: тик упал")

    scheduler.add_job(
        _job,
        "interval",
        seconds=30,
        coalesce=True,
        max_instances=1,
        misfire_grace_time=120,
        id=_BLACKJACK_TIMEOUTS_JOB_ID,
        replace_existing=True,
    )


# --- crash (real-time раунд: живой множитель по wall-clock времени, D-03) ----
#
# Провably-fair распределение точки краша + вся денежная арифметика от неё —
# `crash_engine.py` (чистый модуль, см. его докстринг: RTP-инвариант
# 1-CRASH_HOUSE_EDGE независимо от того, на каком множителе кэшаутится
# игрок, и почему ВСЯ цепочка "точка краша -> прошедшее время -> текущий
# множитель -> выплата" обязана оставаться Decimal от первого до последнего
# шага — аудит-урок dice_fair_payout, 2026-08-06).
#
# Стейтфул-раунд устроен буквально "по образцу" блэкджека (см. секцию выше):
# тот же CasinoGame (game="crash"), тот же state JSONB, тот же переход
# status "active"->"settled" как гард идемпотентности, тот же 4-way SELECT
# ... FOR UPDATE фильтр (id/user_id/chat_id/game) в crash_cash_out, что и
# blackjack_action, по той же IDOR-причине (см. её докстринг). Единственная
# структурная разница — крутится не через клиентское действие ("hit"/
# "stand"), а через ЧИСТОЕ ПРОШЕДШЕЕ ВРЕМЯ, посчитанное сервером, поэтому у
# crash нет ветвлений по action и нет client-supplied данных о ходе раунда
# вообще: единственные входы crash_cash_out — chat_id/game_id/user_id
# (auth-derived).
#
# JSONB-готча (повторяется на каждое чтение/запись ниже, будь внимателен):
# `state["crash_point"]` хранится ТОЛЬКО как `str(Decimal(...))` — JSONB не
# может сериализовать Decimal напрямую — и восстанавливается обратно через
# `Decimal(state["crash_point"])` на каждом чтении.


def _crash_view(game_row: CasinoGame) -> dict:
    """Публичный вид раунда: пока `status=="active"` — ТОЛЬКО id/status/
    bet/started_at, `crash_point` НИКОГДА не раскрывается, пока раунд
    активен (та же дисциплина "спрятать правду до settle", что скрытая
    вторая карта дилера в `_blackjack_view` — иначе клиент мог бы просто
    прочитать точку краша и кэшаутиться за миллисекунду до неё без всякого
    риска, что убивает саму игру). После settle — добавляются
    `crash_point`/`cashed_out_multiplier`/`payout`/`outcome`.

    `crash_point`/`cashed_out_multiplier` — JSON-safe строки (не Decimal,
    JSONB и так уже хранит их как str, но `str(...)` здесь идёт явно и
    защищённо от вызывающего)."""
    state = game_row.state or {}
    view: dict = {
        "id": game_row.id,
        "status": game_row.status,
        "bet": game_row.bet,
        "started_at": state.get("started_at"),
    }
    if game_row.status != "active":
        outcome = game_row.outcome or {}
        view["crash_point"] = str(state["crash_point"])
        view["cashed_out_multiplier"] = (
            outcome.get("multiplier") if outcome.get("result") == "cashed_out" else None
        )
        view["payout"] = game_row.payout
        view["outcome"] = outcome
    return view


async def start_crash(
    session: AsyncSession, chat_id: int, user_id: int, bet: int, idem_key: str
) -> dict:
    """Запускает раунд Crash (стейтфул, буквально "по образцу"
    `start_blackjack` — читай его докстринг за полным разбором формы):
    списывает ставку в банк (общий `_debit_stake`), тянет точку краша через
    `crash_engine.generate_crash_point(_rng)` — сразу `Decimal`, никогда
    `float` — и кладёт её в `CasinoGame.state` ТОЛЬКО как `str(...)` (JSONB-
    готча, см. докстринг секции выше). Раунд остаётся `status="active"` до
    `crash_cash_out` (или до `resolve_crash_timeouts`, если игрок не
    вернулся) — в отличие от блэкджека, тут нет мгновенного settle-пути
    (нет аналога "натурала"): любая новая ставка стартует активный раунд,
    множитель которого уже пошёл расти с этой самой секунды.

    Идемпотентна по `idem_key` — повторный вызов возвращает сохранённый
    раунд без повторного движения денег (та же двухуровневая
    идемпотентность, что у `_settle`/`start_blackjack`)."""
    existing = await _find_existing(session, user_id, idem_key)
    if existing is not None:
        logger.info(
            "start_crash: idem_key=%s уже обработан, возвращаем сохранённый раунд",
            idem_key,
        )
        return _crash_view(existing)

    balance = await economy_service.get_balance(session, chat_id, user_id)
    _validate_bet(bet, balance)

    debited = await _debit_stake(session, chat_id, user_id, bet, idem_key)
    if not debited:
        existing = await _find_existing(session, user_id, idem_key)
        if existing is not None:
            return _crash_view(existing)
        raise DuplicateRound(f"Раунд уже обрабатывается конкурентным запросом (idem_key={idem_key})")

    crash_point = crash_engine.generate_crash_point(_rng)
    state = {
        "crash_point": str(crash_point),
        "started_at": datetime.utcnow().isoformat(),
    }

    game_row = CasinoGame(
        chat_id=chat_id,
        user_id=user_id,
        game="crash",
        bet=bet,
        payout=0,
        outcome=None,
        state=state,
        status="active",
        idem_key=idem_key,
    )
    try:
        session.add(game_row)
        await session.flush()
    except IntegrityError:
        # Партиал-UNIQUE (user_id, idem_key) — backstop против гонки между
        # SELECT-проверкой выше и flush здесь (форма _settle/start_blackjack).
        await session.rollback()
        existing = await _find_existing(session, user_id, idem_key)
        if existing is None:
            raise
        return _crash_view(existing)

    await session.commit()
    return _crash_view(game_row)


def _crash_live_mult(state: dict) -> tuple[Decimal, Decimal]:
    """elapsed -> live_mult, вынесено из `crash_cash_out`, чтобы `peek_crash`
    (см. ниже) считал по ТОМУ ЖЕ Decimal-пути, а не отдельной чуть
    отличающейся копией (урок dice_fair_payout про дублирующуюся денежную
    арифметику, см. докстринг секции выше). Возвращает `(live_mult,
    crash_point)`, обе Decimal."""
    started_at = datetime.fromisoformat(state["started_at"])
    delta = datetime.utcnow() - started_at
    # Аудит-находка: timedelta.total_seconds() возвращает float — единственная
    # оставшаяся float-щель в цепочке "точка краша -> прошедшее время ->
    # множитель -> выплата", которую crash_engine.py's докстринг заявляет
    # полностью Decimal-чистой (D-06-класса урок дайса). days/seconds/
    # microseconds — все целые int по контракту timedelta, поэтому их сумма
    # в Decimal точна, без единого промежуточного float.
    elapsed = (
        Decimal(delta.days) * 86400 + Decimal(delta.seconds) + Decimal(delta.microseconds) / 1_000_000
    )
    if elapsed < 0:
        # Защита от отрицательного elapsed (напр. сдвиг системных часов между
        # двумя utcnow()) — multiplier_at(elapsed<0) поднял бы ValueError на
        # легитимном запросе; defense in depth, в норме не должно случаться.
        elapsed = Decimal(0)

    live_mult = crash_engine.multiplier_at(elapsed)
    crash_point = Decimal(state["crash_point"])
    return live_mult, crash_point


async def peek_crash(session: AsyncSession, chat_id: int, game_id: int, user_id: int) -> dict:
    """Read-only опрос текущего раунда Crash для клиентского polling —
    НИКОГДА не пишет в БД (нет `FOR UPDATE`, нет `commit`, нет
    `pay_from_bank`), можно дёргать часто без риска случайно кэшаутить
    активный раунд самому.

    Баг-репорт 2026-08-07 (Михаил): косметический тикер на клиенте не имел
    НИКАКОГО способа узнать, что раунд уже реально лопнул на сервере —
    `crash_point` намеренно скрыт, пока раунд активен (см. докстринг
    `_crash_view`), поэтому клиент рисовал растущий множитель ЕЩЁ ДОЛГО
    ПОСЛЕ настоящего краша (наблюдался разрыв ~10с — экран показывал ×8,
    хотя раунд реально лопнул на ×1.4 секунды спустя старта), и игрок
    узнавал правду только по факту нажатия ЗАБРАТЬ. `peek_crash` даёт
    клиенту дешёвый способ раз в несколько сотен мс спросить "уже лопнуло?"
    без изменения состояния — если да, клиент сам вызывает настоящий
    `crash_cash_out` (тот единственный код, который реально settle'ит и
    платит; идемпотентен при гонке с ручным нажатием ЗАБРАТЬ — статус-гард
    "active"->"settled" тот же самый).

    Раскрывает `crash_point`/`outcome` ТОЛЬКО если раунд УЖЕ реально settled
    в БД (кем-то другим — живым кэшаутом или таймаут-джобой, тот же путь,
    что `_crash_view`). Если раунд всё ещё формально "active", но
    `elapsed` уже прошёл точку краша — возвращает `pending_bust: true` и
    БОЛЬШЕ НИЧЕГО (сам `crash_point` этим путём не раскрывается, чтобы не
    заводить второй источник правды о его значении — единственное место,
    которое реально его раскрывает, это `_crash_view` после настоящего
    settle через `crash_cash_out`)."""
    game_row = (
        await session.execute(
            select(CasinoGame).where(
                CasinoGame.id == game_id,
                CasinoGame.user_id == user_id,
                CasinoGame.chat_id == chat_id,
                CasinoGame.game == "crash",
            )
        )
    ).scalar_one_or_none()
    if game_row is None:
        raise CasinoError(f"Раунд Crash #{game_id} не найден")

    view = _crash_view(game_row)
    if game_row.status != "active":
        return view

    live_mult, crash_point = _crash_live_mult(dict(game_row.state))
    if live_mult >= crash_point:
        view["pending_bust"] = True
    return view


async def crash_cash_out(session: AsyncSession, chat_id: int, game_id: int, user_id: int) -> dict:
    """Кэшаут активного раунда Crash. `SELECT ... FOR UPDATE` фильтрует по
    ВСЕМ ЧЕТЫРЁМ — `id`/`user_id`/`chat_id`/`game` (то же самое IDOR-
    обоснование, что `blackjack_action`, читай его докстринг за полным
    разбором: без `chat_id` в фильтре игрок с активным раундом в чате A мог
    бы подставить `chat_id` чата B, где он тоже участник, и кэшаутить (со
    своим же `user_id`, но чужим `chat_id`) раунд — выплата всё равно ушла
    бы в РЕАЛЬНЫЙ `game_row.chat_id` (чат A), так что прямой денежной дыры
    тут нет, но фильтр всё равно обязателен: без него запрос из чата B
    просто "нашёл" бы чужую по контексту строку по одному user_id, что уже
    само по себе IDOR — раунд обязан быть виден/управляем только из ТОГО
    чата, где он был начат).

    Статус-переход "active"->"settled" сам служит гардом идемпотентности
    (T-04.1-09-форма, как в `blackjack_action`): повторный вызов на уже
    settled раунде — no-op, возвращает сохранённый исход, деньги не
    двигаются повторно.

    Прошедшее время считается ТОЛЬКО СЕРВЕРОМ: `elapsed = utcnow() -
    state["started_at"]`. Никакой параметр запроса не может сообщить
    elapsed/текущий множитель/что-либо о прогрессе раунда — единственные
    входы этой функции — `chat_id`/`game_id`/`user_id` (все auth-derived),
    в точности как блэкджек никогда не доверяет клиентскому состоянию карт
    (T-04.1-08/D-03).

    Если live-множитель (`crash_engine.multiplier_at(elapsed)`) СТРОГО
    меньше сохранённой точки краша — игрок успел кэшаутиться до краша,
    `payout = int(bet * live_mult)` (Decimal-арифметика вплоть до самого
    `int()`, D-06 через `pay_from_bank`). Иначе (в т.ч. равенство — на
    мгновенном краше `multiplier_at(0) == crash_point == 1.00`, а
    `1.00 < 1.00` ложно) раунд уже лопнул к моменту запроса — `payout=0`,
    `pay_from_bank` не вызывается вовсе (нечего платить)."""
    game_row = (
        await session.execute(
            select(CasinoGame)
            .where(
                CasinoGame.id == game_id,
                CasinoGame.user_id == user_id,
                CasinoGame.chat_id == chat_id,
                CasinoGame.game == "crash",
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if game_row is None:
        raise CasinoError(f"Раунд Crash #{game_id} не найден")

    if game_row.status != "active":
        # Гард идемпотентности: повторный кэшаут на уже settled раунде —
        # no-op, возвращаем сохранённый исход.
        await session.commit()
        return _crash_view(game_row)

    state = dict(game_row.state)
    live_mult, crash_point = _crash_live_mult(state)

    if live_mult < crash_point:
        payout = int(Decimal(game_row.bet) * live_mult)
        paid = await economy_service.pay_from_bank(
            session,
            game_row.chat_id,
            game_row.user_id,
            payout,
            kind="casino_payout",
            ref_id=f"casino:crash:{game_row.id}",
        )
        game_row.payout = paid
        game_row.outcome = {"result": "cashed_out", "multiplier": str(live_mult)}
    else:
        game_row.payout = 0
        game_row.outcome = {"result": "busted", "multiplier": str(crash_point)}

    game_row.status = "settled"
    await session.commit()
    return _crash_view(game_row)


# --- Таймаут-резолвер crash (игрок не вернулся кэшаутиться) + APScheduler ----


async def resolve_crash_timeouts(session: AsyncSession) -> int:
    """Сканирует активные раунды Crash, чья точка краша по РЕАЛЬНОМУ
    wall-clock времени точно уже пройдена (`now() > started_at +
    seconds_to_reach(crash_point) + 3с буфер`), и форс-settle'ит их как
    busted (`payout=0`) — игрок, который не вернулся кэшаутиться (закрыл
    мини-апп/уронил сеть), теряет ставку так же, как если бы дождался
    краша сам; ставка никогда не замораживается навсегда (тот же дух, что
    авто-стенд блэкджека на таймауте, D-07/D-08).

    3-секундный буфер защищает легитимный кэшаут-запрос, летящий В МОМЕНТ
    краша, от гонки с этим джобом — раунд форс-settleится только когда
    реально пройдена не только сама точка краша, но и запас времени сверху.

    Кандидаты на форс-settle отбираются В ДВА ШАГА (в отличие от
    `resolve_blackjack_timeouts`, где готовый `state.turn_deadline` уже
    лежит в state и сравнивается прямо в SQL WHERE): здесь такого
    предвычисленного дедлайна нет, только `crash_point`, из которого
    дедлайн ещё нужно посчитать через `crash_engine.seconds_to_reach` —
    проще и надёжнее сделать это ОДИН РАЗ в Python, тем же самым кодом
    движка, что считает и сам множитель, чем задваивать формулу набором
    сырого SQL-текста. Шаг 1 (без лока) читает ВСЕ активные раунды Crash и
    Python-ом решает, какие реально просрочены. Шаг 2 — для каждого
    просроченного id: свежий `SELECT ... FOR UPDATE` + ПЕРЕПРОВЕРКА статуса
    ПОСЛЕ лока (раунд мог уже settle'иться живым `crash_cash_out` между
    шагом 1 и этим моментом) — та же per-row try/except-continue
    резилентность и та же защита от гонки батч-commit'ов, что
    `resolve_blackjack_timeouts` (читай его докстринг за полным разбором,
    почему батч-wide `FOR UPDATE` здесь намеренно не используется).
    Возвращает число реально форс-settled раундов."""
    rows = (
        await session.execute(
            select(CasinoGame.id, CasinoGame.state).where(
                CasinoGame.game == "crash",
                CasinoGame.status == "active",
            )
        )
    ).all()

    now = datetime.utcnow()
    overdue_ids: list[int] = []
    for game_id, state in rows:
        crash_point = Decimal(state["crash_point"])
        started_at = datetime.fromisoformat(state["started_at"])
        deadline = started_at + timedelta(
            seconds=float(crash_engine.seconds_to_reach(crash_point)) + 3
        )
        if now > deadline:
            overdue_ids.append(game_id)

    resolved_count = 0
    for game_id in overdue_ids:
        try:
            game_row = (
                await session.execute(
                    select(CasinoGame).where(CasinoGame.id == game_id).with_for_update()
                )
            ).scalar_one_or_none()
            if game_row is None or game_row.status != "active":
                # Уже settle'ился (live-кэшаут игрока) между шагом 1 и этим
                # моментом — не наш случай, no-op.
                await session.commit()
                continue

            state = dict(game_row.state)
            crash_point = Decimal(state["crash_point"])

            game_row.payout = 0
            game_row.outcome = {"result": "busted", "multiplier": str(crash_point)}
            game_row.status = "settled"
            await session.commit()
            resolved_count += 1
        except Exception:  # noqa: BLE001 - один застрявший раунд не должен ронять весь тик
            logger.exception(
                "resolve_crash_timeouts: не удалось форс-settle'ить game_id=%s", game_id
            )
            await session.rollback()

    return resolved_count


_CRASH_TIMEOUTS_JOB_ID = "crash_timeouts"


def register_crash_timeouts(scheduler: AsyncIOScheduler) -> None:
    """Регистрирует форс-settle просроченных раундов Crash как interval-job
    (15с — раунды короткоживущие, до `CRASH_MAX_MULTIPLIER` естественный
    максимум ожидания ~26.6с + 3с буфер, частый скан оправдан), по образцу
    `register_blackjack_timeouts`: своя сессия, broad-except — тик обязан
    пережить любую ошибку и не уронить планировщик."""

    async def _job() -> None:
        async with SessionLocal() as session:
            try:
                resolved_count = await resolve_crash_timeouts(session)
                if resolved_count:
                    logger.info(
                        "crash_timeouts: форс-settle просроченных раундов — %s", resolved_count
                    )
            except Exception:  # noqa: BLE001 - job обязан пережить любую ошибку и не уронить планировщик
                logger.exception("crash_timeouts: тик упал")

    scheduler.add_job(
        _job,
        "interval",
        seconds=15,
        coalesce=True,
        max_instances=1,
        misfire_grace_time=60,
        id=_CRASH_TIMEOUTS_JOB_ID,
        replace_existing=True,
    )
