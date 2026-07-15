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
from datetime import datetime
from datetime import timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import settings
from bot.services import blackjack_engine
from bot.services import economy_service
from bot.services import slot_engine
from common.db.session import SessionLocal
from common.models.casino_game import CasinoGame

logger = logging.getLogger(__name__)

_rng = secrets.SystemRandom()

# --- Формулы игр (D-03) ------------------------------------------------------

COINFLIP_MULT = 1.98
DICE_HOUSE_EDGE = 0.02
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
    -> `CasinoGame`), здесь — только сборка `compute()`-замыкания."""
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
        }

    return await _settle(session, chat_id, user_id, "slots", bet, idem_key, compute)


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
    повторно.

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
