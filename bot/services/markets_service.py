"""Жизненный цикл parimutuel-рынков ставок (BET-01) — создание, ставки,
read-хелперы для просмотра.

Деньги двигает ТОЛЬКО через `bot.services.economy_service` (debit/credit_bank)
— этот модуль НИКОГДА не пишет user_balance/chat_bank/economy_tx напрямую
(economy_service.py — единственный модуль с таким правом, см. его докстринг).

Инварианты (RESEARCH.md «Money-Integrity Patterns», «Parimutuel Payout Math»):
- Лимиты создания рынка — D-05 (вопрос 5-400 симв., 2-6 вариантов, длительность
  5м-365д, комиссия создания 100 ювиков в банк чата).
- `place_bet` берёт `SELECT market ... FOR UPDATE` ПЕРВЫМ (контракт порядка
  блокировок, шаг 1) — сериализует все ставки на конкретный рынок, поэтому
  инкремент пула варианта не гонится с другой параллельной ставкой.
- Идемпотентность разовых операций — тот же `ref_id` + SAVEPOINT, что и у
  economy_service (Pattern 2): `economy_service.debit` возвращает False на
  повторный ref_id — операция становится no-op, деньги не двигаются повторно.
- Резолюция/выплаты (`resolve_market`/`cancel_market`) — вне scope этого
  плана (план 03-05).
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from datetime import timedelta

from sqlalchemy import func
from sqlalchemy import select
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import settings
from bot.services import economy_service
from common.models.bet import Bet
from common.models.market import Market
from common.models.market import MarketOption

logger = logging.getLogger(__name__)


# --- Исключения --------------------------------------------------------


class MarketError(Exception):
    """Базовое исключение модуля рынков ставок."""


class MarketNotFound(MarketError):
    """Рынок с указанным id не найден в этом чате."""


class MarketClosed(MarketError):
    """Рынок закрыт для ставок (status != open или истёк closes_at)."""


class DurationError(ValueError):
    """Некорректный формат строки длительности (`"7d|12h|90m"`)."""


class InvalidMarketArg(MarketError):
    """Невалидные входные данные операции (вопрос/варианты/сумма ставки)."""


class DuplicateRequest(MarketError):
    """Повторный запрос с тем же ref_id уже обработан — идемпотентный no-op."""


# --- Лимиты D-05 (модульные константы) ----------------------------------

QUESTION_MIN_LEN = 5
QUESTION_MAX_LEN = 400
MIN_OPTIONS = 2
MAX_OPTIONS = 6
MIN_DURATION = timedelta(minutes=5)
MAX_DURATION = timedelta(days=365)


# --- parse_duration ("7d|12h|90m") ---------------------------------------

_DURATION_TOKEN_RE = re.compile(r"^(\d+)([dhm])$")
_UNIT_TO_KWARG = {"d": "days", "h": "hours", "m": "minutes"}


def parse_duration(raw: str) -> timedelta:
    """Парсит `'7d|12h|90m'` (дни/часы/минуты через `|`) в `timedelta`.

    Пустые/пробельные токены игнорируются; хотя бы один валидный токен
    обязателен, иначе `DurationError`. Некорректный токен (не `\\d+[dhm]`)
    тоже поднимает `DurationError`.
    """
    total = timedelta()
    found_any = False
    for token in raw.split("|"):
        token = token.strip()
        if not token:
            continue
        match = _DURATION_TOKEN_RE.match(token)
        if not match:
            raise DurationError(
                f"Некорректный формат длительности: '{token}' "
                "(ожидается число+d/h/m, например '7d')"
            )
        value = int(match.group(1))
        total += timedelta(**{_UNIT_TO_KWARG[match.group(2)]: value})
        found_any = True

    if not found_any:
        raise DurationError("Пустая строка длительности")
    return total


# --- create_market (D-05) -------------------------------------------------


async def create_market(
    session: AsyncSession,
    chat_id: int,
    creator_id: int,
    question: str,
    option_labels: list[str],
    duration_raw: str,
    ref_id: str,
    market_type: str = "internal",
    external_url: str | None = None,
    external_id: str | None = None,
) -> Market:
    """Создаёт internal-рынок (или регистрирует импортированный, если вызван
    из будущего `import_market` плана 03-06) с лимитами D-05. Списывает
    комиссию создания (`settings.market_creation_fee`) с создателя в банк
    чата через `economy_service` — сам НЕ пишет балансы.

    Поднимает `InvalidMarketArg`/`DurationError` при нарушении лимитов
    (до любого движения денег), `economy_service.InsufficientFunds` при
    нехватке средств на комиссию, `DuplicateRequest` при повторе ref_id
    (идемпотентный no-op — рынок повторно не создаётся).
    """
    question = question.strip()
    if not (QUESTION_MIN_LEN <= len(question) <= QUESTION_MAX_LEN):
        raise InvalidMarketArg(
            f"Вопрос должен быть от {QUESTION_MIN_LEN} до {QUESTION_MAX_LEN} символов "
            f"(сейчас {len(question)})"
        )

    labels = [label.strip() for label in option_labels]
    if not (MIN_OPTIONS <= len(labels) <= MAX_OPTIONS):
        raise InvalidMarketArg(
            f"Количество вариантов ответа должно быть от {MIN_OPTIONS} до {MAX_OPTIONS} "
            f"(сейчас {len(labels)})"
        )
    if any(not label for label in labels):
        raise InvalidMarketArg("Варианты ответа не могут быть пустыми")

    duration = parse_duration(duration_raw)
    if not (MIN_DURATION <= duration <= MAX_DURATION):
        raise InvalidMarketArg("Длительность рынка должна быть от 5 минут до 365 дней")

    fee = settings.market_creation_fee
    debited = await economy_service.debit(
        session, chat_id, creator_id, fee, kind="market_create_fee", ref_id=ref_id
    )
    if not debited:
        logger.info("create_market: ref_id=%s уже обработан, пропускаем", ref_id)
        raise DuplicateRequest(f"Запрос на создание рынка уже обработан (ref_id={ref_id})")

    await economy_service.credit_bank(
        session, chat_id, fee, kind="market_create_fee", ref_id=f"{ref_id}:bank"
    )

    market = Market(
        chat_id=chat_id,
        type=market_type,
        question=question,
        creator_id=creator_id,
        status="open",
        closes_at=datetime.utcnow() + duration,
        external_url=external_url,
        external_id=external_id,
    )
    session.add(market)
    await session.flush()  # нужен market.id для дочерних market_options

    for position, label in enumerate(labels, start=1):
        session.add(MarketOption(market_id=market.id, label=label, pool=0, position=position))

    await session.commit()
    return market


# --- place_bet (parimutuel, идемпотентно) ---------------------------------


async def place_bet(
    session: AsyncSession,
    chat_id: int,
    market_id: int,
    user_id: int,
    option_position: int,
    amount: int,
    ref_id: str,
) -> Bet | None:
    """Ставит `amount` ювиков на вариант `option_position` рынка `market_id`.

    Контракт порядка блокировок (RESEARCH.md Pattern 1): `SELECT market ...
    FOR UPDATE` ПЕРВЫМ — сериализует все ставки на этот рынок, поэтому
    инкремент пула ниже безопасен без отдельной блокировки market_options.

    Возвращает `None`, если `ref_id` уже был обработан ранее (идемпотентный
    no-op — деньги не списываются, пул не растёт повторно). Поднимает
    `InvalidMarketArg` при сумме ниже `settings.market_min_bet` или
    несуществующем варианте, `MarketNotFound`/`MarketClosed` при проблемах
    с рынком, `economy_service.InsufficientFunds` при нехватке средств.
    """
    if amount < settings.market_min_bet:
        raise InvalidMarketArg(f"Минимальная ставка — {settings.market_min_bet} ювиков")

    market = (
        await session.execute(
            select(Market)
            .where(Market.chat_id == chat_id, Market.id == market_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if market is None:
        raise MarketNotFound(f"Рынок #{market_id} не найден")
    if market.status != "open" or market.closes_at <= datetime.utcnow():
        raise MarketClosed(f"Рынок #{market_id} закрыт для ставок")

    option = (
        await session.execute(
            select(MarketOption).where(
                MarketOption.market_id == market_id, MarketOption.position == option_position
            )
        )
    ).scalar_one_or_none()
    if option is None:
        raise InvalidMarketArg(f"Нет варианта №{option_position} в рынке #{market_id}")

    debited = await economy_service.debit(
        session, chat_id, user_id, amount, kind="bet", ref_id=ref_id
    )
    if not debited:
        logger.info("place_bet: ref_id=%s уже обработан, пропускаем", ref_id)
        await session.commit()
        return None

    await session.execute(
        update(MarketOption).where(MarketOption.id == option.id).values(pool=MarketOption.pool + amount)
    )
    bet = Bet(market_id=market_id, option_id=option.id, user_id=user_id, amount=amount)
    session.add(bet)
    await session.commit()
    return bet


# --- Read-хелперы (без записи) --------------------------------------------


async def get_open_markets(session: AsyncSession, chat_id: int) -> list[dict]:
    """Открытые рынки чата для `/markets`, отсортированы по ближайшему закрытию."""
    rows = (
        await session.execute(
            select(
                Market.id,
                Market.question,
                Market.closes_at,
                func.count(MarketOption.id).label("options_count"),
            )
            .join(MarketOption, MarketOption.market_id == Market.id)
            .where(Market.chat_id == chat_id, Market.status == "open")
            .group_by(Market.id)
            .order_by(Market.closes_at)
        )
    ).all()
    return [
        {
            "id": row.id,
            "question": row.question,
            "closes_at": row.closes_at,
            "options_count": row.options_count,
        }
        for row in rows
    ]


async def get_market_detail(session: AsyncSession, chat_id: int, market_id: int) -> dict:
    """Детали рынка для `/market <id>`: варианты с пулами и долями (%),
    статус, суммарный пул. Поднимает `MarketNotFound`, если рынка нет
    в этом чате."""
    market = (
        await session.execute(
            select(Market).where(Market.chat_id == chat_id, Market.id == market_id)
        )
    ).scalar_one_or_none()
    if market is None:
        raise MarketNotFound(f"Рынок #{market_id} не найден")

    options = (
        await session.execute(
            select(MarketOption)
            .where(MarketOption.market_id == market_id)
            .order_by(MarketOption.position)
        )
    ).scalars().all()
    total_pool = sum(option.pool for option in options)

    return {
        "id": market.id,
        "question": market.question,
        "status": market.status,
        "closes_at": market.closes_at,
        "total_pool": total_pool,
        "options": [
            {
                "position": option.position,
                "label": option.label,
                "pool": option.pool,
                "share_pct": round(option.pool / total_pool * 100, 1) if total_pool else 0.0,
            }
            for option in options
        ],
    }


async def get_user_portfolio(session: AsyncSession, chat_id: int, user_id: int) -> list[dict]:
    """Ставки участника в этом чате для `/portfolio` (все статусы рынков)."""
    rows = (
        await session.execute(
            select(
                Bet.id.label("bet_id"),
                Bet.amount,
                Bet.payout,
                Bet.refunded,
                Market.id.label("market_id"),
                Market.question,
                Market.status.label("market_status"),
                MarketOption.label.label("option_label"),
            )
            .join(Market, Market.id == Bet.market_id)
            .join(MarketOption, MarketOption.id == Bet.option_id)
            .where(Market.chat_id == chat_id, Bet.user_id == user_id)
            .order_by(Bet.created_at.desc())
        )
    ).all()
    return [
        {
            "bet_id": row.bet_id,
            "market_id": row.market_id,
            "question": row.question,
            "option_label": row.option_label,
            "amount": row.amount,
            "payout": row.payout,
            "refunded": row.refunded,
            "market_status": row.market_status,
        }
        for row in rows
    ]
