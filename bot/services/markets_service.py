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
- Резолюция (`resolve_market`) — статус-переход "open"->"resolved" САМ является
  гардом идемпотентности (RESEARCH.md Pattern 3-adjacent): повторный вызов на
  уже resolved-рынке — no-op, без повторных выплат (T-03-19). D-03: остаток
  floor-деления при выплате победителям НИКУДА не зачисляется (ни игрокам, ни
  банку) — покидает оборот безвозвратно. Это значит, что
  `sum(user_balance.balance) + chat_bank.balance` НЕ является инвариантом
  после резолюции рынка (Pitfall 3) — не "чинить" это перенаправлением пыли
  в банк, иначе комиссия BET-03 тихо вырастет сверх заявленных 5%.
"""

from __future__ import annotations

import logging
import math
import re
from datetime import datetime
from datetime import timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import func
from sqlalchemy import select
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import settings
from bot.services import economy_service
from bot.services import external_markets
from common.db.session import SessionLocal
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


class MarketAlreadyImported(MarketError):
    """Этот внешний рынок (chat_id, type, external_id) уже был импортирован
    ранее — дедуп (T-03-24), комиссия импорта повторно не списывается."""


# --- Лимиты D-05 (модульные константы) ----------------------------------

QUESTION_MIN_LEN = 5
QUESTION_MAX_LEN = 400
MIN_OPTIONS = 2
MAX_OPTIONS = 6
# common/models/market.py: MarketOption.label — String(200). Проверяем ДО
# INSERT (найдено ревью 2026-07-23, T-BET-01-create-миниапп): без этой
# проверки лейбл длиннее колонки падал уже на session.commit() Postgres'ным
# DataError — НЕ IntegrityError, поэтому ничем в этом модуле/роуте не
# ловился и всплывал наружу необработанным 500 вместо документированного
# InvalidMarketArg -> 400 (тот же класс валидации, что уже есть у question
# выше — длина лейбла проверяется здесь же, ДО любого движения денег).
OPTION_LABEL_MAX_LEN = 200
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
    if any(len(label) > OPTION_LABEL_MAX_LEN for label in labels):
        raise InvalidMarketArg(f"Вариант ответа не длиннее {OPTION_LABEL_MAX_LEN} символов")

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


# --- import_market (BET-02) ------------------------------------------------

# Внешний рынок резолвится источником (auto_resolve_external), не локальным
# closes_at — 365д здесь лишь предохранитель авто-закрытия auto_close_expired
# на случай, если внешний источник никогда не закроется/не совпадёт по label.
_IMPORT_MARKET_SAFETY_DURATION = timedelta(days=365)


async def import_market(
    session: AsyncSession, chat_id: int, creator_id: int, url: str, ref_id: str
) -> Market:
    """Импортирует рынок Polymarket/Manifold по `url` (BET-02): фетчит и
    нормализует через `external_markets.fetch_external_market` (весь HTTP/
    SSRF-риск изолирован там, T-03-23 — этот модуль никогда не вызывает
    aiohttp напрямую), дедуплицирует по `(chat_id, type, external_id)`
    (партиал-UNIQUE `ux_markets_chat_type_external`, T-03-24) и списывает
    комиссию импорта (`settings.market_import_fee`) с импортёра в банк чата.

    Дедуп-проверка выполняется ДО списания комиссии — повторный импорт уже
    существующего внешнего рынка не берёт деньги второй раз, поднимает
    `MarketAlreadyImported`. `UnsupportedMarketUrl`/`MarketFetchError`
    (из `external_markets`) пробрасываются вызывающему как есть.
    """
    fetched = await external_markets.fetch_external_market(url)
    market_type = "polymarket" if "polymarket.com" in url else "manifold"

    existing = (
        await session.execute(
            select(Market).where(
                Market.chat_id == chat_id,
                Market.type == market_type,
                Market.external_id == fetched["external_id"],
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise MarketAlreadyImported(
            f"Этот рынок уже импортирован в этом чате (#{existing.id})"
        )

    fee = settings.market_import_fee
    debited = await economy_service.debit(
        session, chat_id, creator_id, fee, kind="market_import_fee", ref_id=ref_id
    )
    if not debited:
        logger.info("import_market: ref_id=%s уже обработан, пропускаем", ref_id)
        raise DuplicateRequest(f"Запрос на импорт рынка уже обработан (ref_id={ref_id})")

    await economy_service.credit_bank(
        session, chat_id, fee, kind="market_import_fee", ref_id=f"{ref_id}:bank"
    )

    market = Market(
        chat_id=chat_id,
        type=market_type,
        question=fetched["question"],
        creator_id=creator_id,
        status="open",
        closes_at=datetime.utcnow() + _IMPORT_MARKET_SAFETY_DURATION,
        external_url=url,
        external_id=fetched["external_id"],
    )

    try:
        session.add(market)
        await session.flush()  # нужен market.id для дочерних market_options; партиал-UNIQUE проверяется здесь

        for position, label in enumerate(fetched["options"], start=1):
            session.add(MarketOption(market_id=market.id, label=label, pool=0, position=position))

        await session.commit()
    except IntegrityError:
        # Партиал-UNIQUE ux_markets_chat_type_external — backstop против гонки
        # повторного импорта того же рынка между SELECT-проверкой выше и
        # flush/commit (T-03-24). Ничего в этой транзакции ещё не было
        # durable-закоммичено (debit/credit_bank шли через SAVEPOINT внутри
        # той же ещё-не-закоммиченной внешней транзакции), поэтому rollback
        # откатывает и списанную комиссию — повторный вызов с тем же ref_id
        # у следующей попытки не станет двойным списанием.
        await session.rollback()
        raise MarketAlreadyImported(
            f"Этот рынок уже импортирован в этом чате (external_id={fetched['external_id']})"
        ) from None
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
    """Открытые рынки чата для `/markets`, отсортированы по ближайшему закрытию.

    `total_pool` (сумма пулов всех вариантов) добавлена для Mini App списка
    рынков (04.2-07, D-04 — UI-SPEC требует показывать суммарный пул в
    строке списка) — чисто аддитивное read-only поле, `/markets`-форматтер
    (`bot/handlers/markets.py::format_markets_list`) его не использует и не
    ломается от лишнего ключа."""
    rows = (
        await session.execute(
            select(
                Market.id,
                Market.question,
                Market.closes_at,
                func.count(MarketOption.id).label("options_count"),
                func.coalesce(func.sum(MarketOption.pool), 0).label("total_pool"),
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
            "total_pool": row.total_pool,
            "options_count": row.options_count,
        }
        for row in rows
    ]


async def get_market_detail(session: AsyncSession, chat_id: int, market_id: int) -> dict:
    """Детали рынка для `/market <id>`: варианты с пулами и долями (%),
    статус, суммарный пул. Поднимает `MarketNotFound`, если рынка нет
    в этом чате.

    `winning_option_id`/каждого `option["id"]` добавлены для Mini App детали
    рынка (04.2-07, D-04 — UI-SPEC требует Hero-tier reveal выигравшего
    варианта после резолюции) — чисто аддитивные read-only поля, `/market`
    -форматтер (`bot/handlers/markets.py::format_market_detail`) их не
    использует и не ломается от лишних ключей."""
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
        "winning_option_id": market.winning_option_id,
        "options": [
            {
                "id": option.id,
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


# --- resolve_market (BET-03, D-01/D-03) ------------------------------------


async def resolve_market(
    session: AsyncSession, chat_id: int, market_id: int, winning_option_id: int
) -> dict:
    """Резолвит internal-рынок: parimutuel-выплата победителям + 5% комиссия
    в банк (BET-03), той же формулой `max(1, ceil(0.05*pool))`, что и D-04's
    transfer_with_fee. D-03: остаток floor-деления теряется (не игрокам, не
    банку) — см. докстринг модуля/Pitfall 3.

    Контракт порядка блокировок (RESEARCH.md Pattern 1): `markets` строка
    блокируется FIRST (`SELECT ... FOR UPDATE`), замораживая `status` —
    статус-переход "open"->"resolved" САМ служит гардом идемпотентности:
    повторный resolve уже resolved-рынка — no-op (T-03-19), балансы/банк не
    меняются. winner.pool == 0 (никто не поставил на выигравший вариант) —
    полный рефанд всем ставившим (division-by-zero иначе), отдельно от
    D-03 (которое применяется только когда winner.pool > 0). total_pool == 0
    (вообще никто не ставил) — рынок просто закрывается, выплат нет.

    Поднимает `MarketNotFound`, если рынка нет в этом чате; `MarketClosed`,
    если рынок уже отменён (`status == "cancelled"`).
    """
    market = (
        await session.execute(
            select(Market).where(Market.chat_id == chat_id, Market.id == market_id).with_for_update()
        )
    ).scalar_one_or_none()
    if market is None:
        raise MarketNotFound(f"Рынок #{market_id} не найден")
    if market.status == "resolved":
        # Статус-переход — гард идемпотентности (T-03-19): повторный resolve
        # уже resolved-рынка ничего не меняет.
        return {"status": "already_resolved", "market_id": market_id}
    if market.status == "cancelled":
        raise MarketClosed(f"Рынок #{market_id} уже отменён")

    options = (
        await session.execute(
            select(MarketOption).where(MarketOption.market_id == market_id).order_by(MarketOption.id)
        )
    ).scalars().all()
    winner = next((option for option in options if option.id == winning_option_id), None)
    if winner is None:
        raise InvalidMarketArg(f"Нет варианта id={winning_option_id} в рынке #{market_id}")

    total_pool = sum(option.pool for option in options)
    all_bets = (await session.execute(select(Bet).where(Bet.market_id == market_id))).scalars().all()

    winners_count = 0
    total_paid = 0
    fee = 0
    dust = 0

    if total_pool == 0:
        # Никто не ставил вообще — нечего распределять, просто закрываем рынок.
        pass
    elif winner.pool == 0:
        # Выигравший вариант без единой ставки — пропорциональная выплата
        # математически не определена (деление на ноль). Полный рефанд всем,
        # аналогично cancel_market — отдельный кейс от D-03 (тот применяется
        # только когда есть ненулевой пул победителя, из которого распределять).
        for bet in all_bets:
            await economy_service.credit(
                session,
                chat_id,
                bet.user_id,
                bet.amount,
                kind="market_refund",
                ref_id=f"market_refund:{market_id}:{bet.id}",
            )
            bet.payout = bet.amount
            bet.refunded = True
        total_paid = total_pool
    else:
        # BET-03 + D-04: та же формула комиссии, что у /transfer.
        fee = max(1, math.ceil(total_pool * settings.market_resolution_fee_pct))
        distributable = total_pool - fee

        # Контракт порядка блокировок, шаг 3: user_balance по возрастанию user_id.
        winning_bets = sorted(
            (bet for bet in all_bets if bet.option_id == winning_option_id), key=lambda bet: bet.user_id
        )
        for bet in winning_bets:
            # D-03: floor-деление — остаток НИКОМУ не зачисляется (не игроку,
            # не банку). См. докстринг модуля/Pitfall 3.
            payout = (distributable * bet.amount) // winner.pool
            await economy_service.credit(
                session,
                chat_id,
                bet.user_id,
                payout,
                kind="market_payout",
                ref_id=f"market_resolve:{market_id}:{bet.id}",
            )
            bet.payout = payout
            total_paid += payout
            winners_count += 1

        for bet in all_bets:
            if bet.option_id != winning_option_id:
                bet.payout = 0

        dust = distributable - total_paid

        # Контракт порядка блокировок, шаг 4: банк — ПОСЛЕДНИМ.
        await economy_service.credit_bank(
            session, chat_id, fee, kind="market_resolution_fee", ref_id=f"market_resolve_fee:{market_id}"
        )

    market.status = "resolved"
    market.winning_option_id = winning_option_id
    await session.commit()

    return {
        "status": "resolved",
        "market_id": market_id,
        "winning_option_id": winning_option_id,
        "winners_count": winners_count,
        "total_paid": total_paid,
        "fee": fee,
        "dust": dust,
        "total_pool": total_pool,
    }


async def resolve_market_by_position(
    session: AsyncSession, chat_id: int, market_id: int, winning_position: int
) -> dict:
    """Тонкая обёртка над `resolve_market` — переводит 1-based номер варианта
    (как показывает `/market <id>`, RESEARCH.md Assumption A4) в DB `option_id`.
    Нужна отдельно от `resolve_market`, чтобы будущий `auto_resolve_external`
    (план 03-06) мог звать `resolve_market` напрямую с уже известным option_id,
    а админ-команда `/market_resolve` — по человекочитаемому номеру.
    """
    option = (
        await session.execute(
            select(MarketOption).where(
                MarketOption.market_id == market_id, MarketOption.position == winning_position
            )
        )
    ).scalar_one_or_none()
    if option is None:
        raise InvalidMarketArg(f"Нет варианта №{winning_position} в рынке #{market_id}")
    return await resolve_market(session, chat_id, market_id, option.id)


# --- cancel_market (D-02, полный рефанд) ------------------------------------


async def cancel_market(session: AsyncSession, chat_id: int, market_id: int) -> dict:
    """Отменяет рынок с полным рефандом всем ставившим (D-02) — форма
    `resolve_market` без комиссии/выплаты. Статус-переход тоже служит гардом
    идемпотентности: повторная отмена уже cancelled/resolved-рынка — no-op.

    Поднимает `MarketNotFound`, если рынка нет в этом чате.
    """
    market = (
        await session.execute(
            select(Market).where(Market.chat_id == chat_id, Market.id == market_id).with_for_update()
        )
    ).scalar_one_or_none()
    if market is None:
        raise MarketNotFound(f"Рынок #{market_id} не найден")
    if market.status in ("cancelled", "resolved"):
        return {"status": f"already_{market.status}", "market_id": market_id, "refunded_count": 0, "total_refunded": 0}

    all_bets = (await session.execute(select(Bet).where(Bet.market_id == market_id))).scalars().all()

    refunded_count = 0
    total_refunded = 0
    for bet in all_bets:
        await economy_service.credit(
            session,
            chat_id,
            bet.user_id,
            bet.amount,
            kind="market_cancel_refund",
            ref_id=f"market_cancel:{market_id}:{bet.id}",
        )
        bet.payout = bet.amount
        bet.refunded = True
        refunded_count += 1
        total_refunded += bet.amount

    market.status = "cancelled"
    await session.commit()

    return {
        "status": "cancelled",
        "market_id": market_id,
        "refunded_count": refunded_count,
        "total_refunded": total_refunded,
    }


# --- auto_close_expired + APScheduler --------------------------------------


async def auto_close_expired(session: AsyncSession) -> int:
    """Атомарный массовый переход просроченных открытых рынков в "closed".
    Затрагивает только `markets` — контракт порядка блокировок не применим
    (одна таблица). Возвращает число переведённых рынков."""
    stmt = (
        update(Market)
        .where(Market.status == "open", Market.closes_at <= func.now())
        .values(status="closed")
    )
    result = await session.execute(stmt)
    await session.commit()
    return result.rowcount


_AUTO_CLOSE_JOB_ID = "markets_auto_close"


def register_auto_close(scheduler: AsyncIOScheduler) -> None:
    """Регистрирует фоновый auto-close как interval-job (5 минут), по образцу
    embed_worker.register: своя сессия, broad-except — тик обязан пережить
    любую ошибку и не уронить планировщик (T-03-21)."""

    async def _job() -> None:
        async with SessionLocal() as session:
            try:
                closed_count = await auto_close_expired(session)
                if closed_count:
                    logger.info("markets_auto_close: закрыто просроченных рынков — %s", closed_count)
            except Exception:  # noqa: BLE001 - job обязан пережить любую ошибку и не уронить планировщик
                logger.exception("markets_auto_close: тик упал")

    scheduler.add_job(
        _job,
        "interval",
        minutes=5,
        coalesce=True,
        max_instances=1,
        misfire_grace_time=120,
        id=_AUTO_CLOSE_JOB_ID,
        replace_existing=True,
    )


# --- auto_resolve_external (BET-02 + BET-03) + APScheduler -----------------


async def auto_resolve_external(session: AsyncSession) -> None:
    """Сверяет открытые/закрытые внешние (polymarket/manifold) рынки с
    источником и авторезолвит те, что закрылись с явным победителем.

    Per-market try/except: transient fetch-ошибка (сеть/таймаут/API вниз)
    НЕ автоотменяет рынок — просто continue, ретрай следующим тиком (T-03-25).
    Источник ещё торгуется (`not closed`) или закрылся без явного победителя
    (`winning_label is None`) — тоже continue, ждём следующего тика.

    Матчинг `winning_label`→`option.label` — casefold-сравнение с обрезкой
    пробелов. Несовпадение ни с одним вариантом — `logger.warning` + skip:
    НЕ гадаем, какой вариант считать победившим (T-03-25) — рынок ждёт
    вмешательства человека (ручной `/market_resolve` админом).

    Резолюция идёт через `resolve_market` — та же parimutuel-выплата с 5%
    комиссией в банк, что и у ручной резолюции internal-рынков (BET-03).
    """
    external_markets_rows = (
        await session.execute(
            select(Market).where(
                Market.status.in_(("open", "closed")),
                Market.type.in_(("polymarket", "manifold")),
            )
        )
    ).scalars().all()

    for market in external_markets_rows:
        try:
            fetched = await external_markets.fetch_external_market(market.external_url)
        except Exception:  # noqa: BLE001 - transient-сбой не должен ронять весь тик или отменять рынок
            logger.exception(
                "auto_resolve_external: не удалось получить рынок market_id=%s", market.id
            )
            continue

        if not fetched["closed"] or fetched["winning_label"] is None:
            continue

        options = (
            await session.execute(select(MarketOption).where(MarketOption.market_id == market.id))
        ).scalars().all()
        normalized_target = fetched["winning_label"].strip().casefold()
        winner = next(
            (option for option in options if option.label.strip().casefold() == normalized_target),
            None,
        )
        if winner is None:
            logger.warning(
                "auto_resolve_external: нет варианта, совпадающего с label=%r, market_id=%s",
                fetched["winning_label"],
                market.id,
            )
            continue

        await resolve_market(session, market.chat_id, market.id, winner.id)


_EXTERNAL_CHECK_JOB_ID = "external_markets_check"


def register_external_check(scheduler: AsyncIOScheduler) -> None:
    """Регистрирует фоновую сверку внешних рынков как interval-job (30 минут),
    по образцу register_auto_close/embed_worker.register: своя сессия,
    broad-except — тик обязан пережить любую ошибку и не уронить планировщик
    (T-03-27)."""

    async def _job() -> None:
        async with SessionLocal() as session:
            try:
                await auto_resolve_external(session)
            except Exception:  # noqa: BLE001 - job обязан пережить любую ошибку и не уронить планировщик
                logger.exception("external_markets_check: тик упал")

    scheduler.add_job(
        _job,
        "interval",
        minutes=30,
        coalesce=True,
        max_instances=1,
        misfire_grace_time=600,
        id=_EXTERNAL_CHECK_JOB_ID,
        replace_existing=True,
    )
