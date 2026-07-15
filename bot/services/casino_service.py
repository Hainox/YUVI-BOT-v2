"""Идемпотентное денежное ядро казино (04.1) — settle-раунд, поверх которого
построены coinflip/dice/roulette (слоты и блэкджек — 04.1-02/03, переиспользуют
`_settle`/`pay_from_bank` отсюда).

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

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import settings
from bot.services import economy_service
from common.models.casino_game import CasinoGame

logger = logging.getLogger(__name__)

_rng = secrets.SystemRandom()

# --- Формулы игр (D-03) ------------------------------------------------------

COINFLIP_MULT = 1.98
DICE_HOUSE_EDGE = 0.02
ROULETTE_NUMBER_MULT = 36
ROULETTE_EVEN_MULT = 2
ROULETTE_DOZEN_MULT = 3

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
    для всех игр, ref_id производный от idem_key. Возвращает False, если
    debit уже был применён ранее для этого idem_key (replay)."""
    debited = await economy_service.debit(
        session, chat_id, user_id, bet, kind="casino_bet", ref_id=f"casino:{idem_key}"
    )
    if not debited:
        return False
    await economy_service.credit_bank(
        session, chat_id, bet, kind="casino_bet", ref_id=f"casino:{idem_key}:bank"
    )
    return True


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
    Выплата на выигрыш — `int(bet * (1 - DICE_HOUSE_EDGE) / win_prob)`."""
    if direction not in ("over", "under"):
        raise InvalidBet("direction должен быть 'over' или 'under'")
    if not (2 <= target <= 99):
        raise InvalidBet("target должен быть в диапазоне 2..99")

    def compute() -> tuple[int, dict]:
        roll = _rng.randint(1, 100)
        if direction == "under":
            win_prob = (target - 1) / 100
            won = roll < target
        else:
            win_prob = (100 - target) / 100
            won = roll > target
        payout = int(bet * (1 - DICE_HOUSE_EDGE) / win_prob) if won else 0
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

    def compute() -> tuple[int, dict]:
        spin = _rng.randint(0, 36)
        won, mult = _roulette_win(bet_type, bet_value, spin)
        payout = int(bet * mult) if won else 0
        return payout, {"spin": spin, "bet_type": bet_type, "bet_value": bet_value, "won": won}

    return await _settle(session, chat_id, user_id, "roulette", bet, idem_key, compute)
