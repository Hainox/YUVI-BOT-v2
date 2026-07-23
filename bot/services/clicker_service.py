"""Ферма-кликер (04.1) — тапы, оффлайн-автокликер, CP-апгрейды, AMM CP<->ювик.

CP (`ClickerFarm.cp`) — ферма-внутренняя валюта. Тапы/апгрейды НИКОГДА не
трогают ювики. Начиная с плана 04.1-05 этот модуль ДОПОЛНИТЕЛЬНО становится
мостом CP<->ювик (`convert_cp`/`buy_cp`/`amm_tick`) — но двигает ювики
ИСКЛЮЧИТЕЛЬНО через `bot.services.economy_service` (`credit`/`debit`/
`credit_bank`), НИКОГДА не пишет `user_balance`/`chat_bank`/`economy_tx`
напрямую (тот же хард-инвариант, что у `markets_service.py`, см. его
докстринг).

Формулы фермы — D-03 (`04-CONTEXT.md`) + REFERENCE-XYLOZ.md §3.1 (`CLICKER_*`
константы эталона xyloz_tg_bot), переносятся точно:
- Анти-чит тапов (T-04.1-12): клиентский `count` НИКОГДА не доверяем напрямую
  — `accepted = min(count, max(1, int(MAX_CPS*elapsed_ms/1000)))`.
- Оффлайн-накопление автокликера (T-04.1-13): считается НА КАЖДОМ обращении
  (`_accrue_offline`), а не фоновым тиком на юзера — `elapsed` берётся из
  разницы `now - last_accrued_at` (серверных значений), клиент не может
  подделать elapsed для начисления; капается `MAX_OFFLINE_SECONDS` (4ч).
- Стоимость апгрейда (T-04.1-14): `int(round(base * UPGRADE_GROWTH**level))`,
  считается сервером, при нехватке CP апгрейд отклоняется `ClickerError`.

AMM CP<->ювик (D-03, REFERENCE-XYLOZ.md §3.1 `market_service.py`) — per-чат
constant-product пул (`ClickerMarketPool.r_cp * r_h = k`), слиппедж встроен
в саму кривую, плюс фоновый mean-reversion тик (~10 мин) тянет цену к якорю
(100 CP/ювик) и пишет снапшот (`ClickerMarketPrice`) для графика. Резервы и
цена — `Decimal`/Numeric(20,8), НИКОГДА float (CR-03: плавающая точка
накапливает погрешность округления при повторных умножениях/делениях
constant-product). Пул блокируется `SELECT ... FOR UPDATE` до любой мутации
резервов (T-04.1-15) — свопы и тик сериализуются на строке пула.
"""

from __future__ import annotations

import logging
import math
from datetime import datetime
from decimal import Decimal

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from bot.services import economy_service
from bot.services import gacha_catalog
from common.db.session import SessionLocal
from common.models.clicker_farm import ClickerFarm
from common.models.clicker_market_pool import ClickerMarketPool
from common.models.clicker_market_price import ClickerMarketPrice
from common.models.gacha_collection import GachaCollection

logger = logging.getLogger(__name__)

# --- Формулы фермы (D-03, REFERENCE-XYLOZ.md §3.1 CLICKER_*) -----------------

MAX_CPS = 30  # анти-чит тапов: сервер клэмпит принятые тапы этим потолком (клиент троттлит до 20)
MAX_OFFLINE_SECONDS = 4 * 3600  # 4ч — кап оффлайн-накопления автокликера (OFFLINE_CAP_HOURS)

TAP_UPGRADE_BASE = 50  # REFERENCE-XYLOZ.md §3.1: TAP_UPGRADE_BASE=50
AUTO_UPGRADE_BASE = 200  # REFERENCE-XYLOZ.md §3.1: AUTO_UPGRADE_BASE=200
UPGRADE_GROWTH = 1.15  # REFERENCE-XYLOZ.md §3.1: UPGRADE_GROWTH=1.15

AUTO_CP_PER_LEVEL_PER_SEC = 0.5  # REFERENCE-XYLOZ.md §3.1: AUTO_RATE=0.5 (CP/сек на 1 уровень автокликера)

# Claude's discretion (эталон не задаёт точную CP-цену тапа явно): CP за один
# принятый тап на tap_level=1; растёт линейно с tap_level (см. tap_value()).
TAP_CP_BASE = 1

# --- GACHA-02: доход фермы от собранных worker-персонажей --------------------
#
# REFERENCE-XYLOZ.md §3.1 описывает 5 именных РАБОТНИЦ, покупаемых напрямую за
# ювики (W_CHERRY/W_LEMON/W_BELL/W_STAR/W_DIAMOND, COST/RATE=50/0.2,
# 250/0.5, 1200/1.5, 6000/5, 30000/20) — отдельная от гачи механика эталона.
# В v2 (04.1-06, D-07) вместо неё уже стоит 4-тирный гача-каталог
# (R/SR/SSR/UR) с полем role: "worker"|"heroine" на персонаже, явно намекающим
# на будущую связь с доходом фермы, но САМА формула отсутствовала до этого
# плана (04.2-RESEARCH.md Open Question Q1/A3). Claude's discretion (структура
# 5 именных работниц 1:1 не переносима на 4-тирный каталог): RATE эталона
# портируется ПО ПОРЯДКУ на тиры каталога (Cherry->R, Lemon->SR, Bell->SSR,
# Star->UR) — числа 0.2/0.5/1.5/5 те же, что в эталоне; пятое значение
# (Diamond=20) не используется, в каталоге всего 4 тира. R недостижим через
# /roll (D-07), но каталог-only персонажи всё ещё могут быть получены другими
# путями в будущем (Фаза 5) — ставка сохранена на будущее.
WORKER_TIER_CP_PER_SEC: dict[str, float] = {
    "R": 0.2,
    "SR": 0.5,
    "SSR": 1.5,
    "UR": 5.0,
}

# --- AMM CP<->ювик (D-03, REFERENCE-XYLOZ.md §3.1 market_service.py) --------

# FARM "100 CP = 1 ювик" (FARM-01) — теперь якорь mean-reversion, а не
# фиксированный курс: REFERENCE-XYLOZ.md §3.1 MARKET_ANCHOR_RATE=100.
AMM_ANCHOR_CP_PER_HRYVNA = 100

# Семенные резервы пула (REFERENCE-XYLOZ.md §3.1 MARKET_R_H0=200000):
# r_h=200_000, r_cp=anchor*r_h=20_000_000 — их отношение равно якорю, значит
# при первом создании чата пул стартует РОВНО на цене якоря (100 CP/ювик).
AMM_SEED_R_H = 200_000
AMM_SEED_R_CP = AMM_ANCHOR_CP_PER_HRYVNA * AMM_SEED_R_H

# REFERENCE-XYLOZ.md §3.1: MARKET_TAU_MIN=240 (минут) -> секунды.
AMM_TAU_SECONDS = 240 * 60
# REFERENCE-XYLOZ.md §3.1: MARKET_TICK_MIN=10.
AMM_TICK_MINUTES = 10

# factor = exp(-(TICK_MINUTES*60)/TAU_SECONDS) зависит только от констант
# выше — считается один раз при импорте модуля, не на каждый тик/своп.
_MEAN_REVERSION_FACTOR = Decimal(str(math.exp(-(AMM_TICK_MINUTES * 60) / AMM_TAU_SECONDS)))


class ClickerError(Exception):
    """Базовое исключение фермы-кликера (CP внутренние, не ювики)."""


def tap_value(tap_level: int) -> int:
    """CP за один принятый тап при данном уровне тапа (растёт линейно)."""
    return TAP_CP_BASE * tap_level


def _upgrade_cost(base: int, level: int) -> int:
    """D-03: стоимость апгрейда — int(round(base * UPGRADE_GROWTH**level))."""
    return int(round(base * UPGRADE_GROWTH**level))


async def _get_or_create_farm(session: AsyncSession, chat_id: int, user_id: int) -> ClickerFarm:
    """Идемпотентный get-or-create строки фермы (мирроит
    `economy_service._get_or_create_balance`): `pg_insert(...)
    .on_conflict_do_nothing` по (chat_id, user_id), затем безусловный
    `SELECT ... FOR UPDATE` — один и тот же ORM-объект `ClickerFarm` на всю
    операцию (tap/upgrade мутируют его атрибуты напрямую перед коммитом)."""
    stmt = (
        pg_insert(ClickerFarm)
        .values(chat_id=chat_id, user_id=user_id)
        .on_conflict_do_nothing(index_elements=["chat_id", "user_id"])
    )
    await session.execute(stmt)

    farm = (
        await session.execute(
            select(ClickerFarm)
            .where(ClickerFarm.chat_id == chat_id, ClickerFarm.user_id == user_id)
            .with_for_update()
        )
    ).scalar_one()
    return farm


async def _collection_income_per_sec(session: AsyncSession, chat_id: int, user_id: int) -> float:
    """GACHA-02: сумма CP/сек от собранных worker-role персонажей —
    `WORKER_TIER_CP_PER_SEC[tier] * gacha_catalog.star_mult(stars)` по каждой
    строке `gacha_collection`, где каталожный `role == "worker"`. Heroine-role
    персонажи НЕ участвуют (тот же фильтр, что и в докстринге
    `gacha_catalog.Character.role`)."""
    rows = (
        await session.execute(
            select(GachaCollection.char_id, GachaCollection.stars).where(
                GachaCollection.chat_id == chat_id, GachaCollection.user_id == user_id
            )
        )
    ).all()
    total = 0.0
    for char_id, stars in rows:
        char = gacha_catalog.CATALOG.get(char_id)
        if char is None or char.role != "worker":
            continue
        total += WORKER_TIER_CP_PER_SEC[char.tier] * gacha_catalog.star_mult(stars)
    return total


async def _accrue_offline(session: AsyncSession, chat_id: int, user_id: int, farm: ClickerFarm) -> float:
    """D-03: оффлайн-накопление на КАЖДОМ обращении, без фонового тика на
    юзера. `elapsed` — разница серверных `last_accrued_at`/`utcnow()`,
    зажатая в [0, MAX_OFFLINE_SECONDS] (нижняя граница — защита от
    отрицательного elapsed при возможном рассинхроне часов). CP/сек —
    `auto_level*AUTO_CP_PER_LEVEL_PER_SEC` ПЛЮС GACHA-02 доход коллекции
    (`_collection_income_per_sec`) — начисление идёт, даже если auto_level=0,
    но коллекция непуста. Возвращает worker_cp_per_sec (для отчёта в
    `get_farm_state`, чтобы не запрашивать коллекцию дважды)."""
    worker_cp_per_sec = await _collection_income_per_sec(session, chat_id, user_id)

    now = datetime.utcnow()
    raw_elapsed = (now - farm.last_accrued_at).total_seconds()
    elapsed = max(0.0, min(raw_elapsed, MAX_OFFLINE_SECONDS))
    total_cp_per_sec = farm.auto_level * AUTO_CP_PER_LEVEL_PER_SEC + worker_cp_per_sec
    if elapsed > 0 and total_cp_per_sec > 0:
        farm.cp += int(total_cp_per_sec * elapsed)
    farm.last_accrued_at = now

    return worker_cp_per_sec


def _farm_state(farm: ClickerFarm, accepted: int | None = None) -> dict:
    state = {
        "cp": farm.cp,
        "tap_level": farm.tap_level,
        "auto_level": farm.auto_level,
    }
    if accepted is not None:
        state["accepted"] = accepted
    return state


async def get_farm_state(session: AsyncSession, chat_id: int, user_id: int) -> dict:
    """Read-путь, который поллит Mini App: get-or-create + оффлайн-накопление
    + коммит + текущее состояние фермы (cp/tap_level/auto_level/cp_per_sec —
    последнее включает GACHA-02 доход коллекции, отображается фронтендом)."""
    farm = await _get_or_create_farm(session, chat_id, user_id)
    worker_cp_per_sec = await _accrue_offline(session, chat_id, user_id, farm)
    await session.commit()
    state = _farm_state(farm)
    state["cp_per_sec"] = farm.auto_level * AUTO_CP_PER_LEVEL_PER_SEC + worker_cp_per_sec
    return state


async def wipe_farm(session: AsyncSession, chat_id: int, user_id: int) -> dict:
    """FARM-03 (/farmwipe): сбрасывает экономику фермы участника к начальным
    значениям (cp=0, tap_level=1, auto_level=0). `pity_ssr`/`pity_ur` и
    `gacha_collection` НЕ трогаются — административный сброс фермы это сброс
    ИМЕННО фермы-кликера (tap/auto/CP), не гача-инвентаря/прогресса пити
    (та же логика разделения ответственности, что и раздельные строки
    `farm.py`/`gacha.py` в этом плане). `last_accrued_at`/`last_tap_at`
    сбрасываются в `utcnow()`, чтобы следующий `get_farm_state`/`tap` не
    накопил оффлайн-CP за интервал до сброса на уже обнулённом auto_level."""
    farm = await _get_or_create_farm(session, chat_id, user_id)
    now = datetime.utcnow()
    farm.cp = 0
    farm.tap_level = 1
    farm.auto_level = 0
    farm.last_accrued_at = now
    farm.last_tap_at = now
    await session.commit()
    return _farm_state(farm)


async def tap(
    session: AsyncSession, chat_id: int, user_id: int, count: int, elapsed_ms: int
) -> dict:
    """Анти-чит тап (D-03/T-04.1-12): `accepted = min(count, max(1,
    int(MAX_CPS*elapsed_ms/1000)))` — клиентский `count` никогда не
    принимается напрямую. `elapsed_ms` тоже не принимается напрямую (CR-02):
    клэмпится сверху реальным серверным интервалом с прошлого принятого тапа
    (`last_tap_at`, пишется ТОЛЬКО этой функцией — в отличие от
    `last_accrued_at`, который сбрасывает каждый poll `get_farm_state`, что
    сделало бы его непригодным для анти-чита тапа). CP растёт на
    `accepted * tap_value(tap_level)`."""
    farm = await _get_or_create_farm(session, chat_id, user_id)
    await _accrue_offline(session, chat_id, user_id, farm)

    now = datetime.utcnow()
    server_elapsed_ms = max(0.0, (now - farm.last_tap_at).total_seconds() * 1000)
    trusted_elapsed_ms = min(elapsed_ms, server_elapsed_ms)
    farm.last_tap_at = now

    accepted = min(count, max(1, int(MAX_CPS * trusted_elapsed_ms / 1000)))
    farm.cp += accepted * tap_value(farm.tap_level)

    await session.commit()
    return _farm_state(farm, accepted=accepted)


async def _upgrade(session: AsyncSession, chat_id: int, user_id: int, base: int, level_attr: str) -> dict:
    """Общее ядро апгрейда тапа/автокликера (T-04.1-14): cost считается ДО
    списания, при нехватке CP апгрейд отклоняется без изменения состояния."""
    farm = await _get_or_create_farm(session, chat_id, user_id)
    await _accrue_offline(session, chat_id, user_id, farm)

    level = getattr(farm, level_attr)
    cost = _upgrade_cost(base, level)
    if farm.cp < cost:
        raise ClickerError(f"Недостаточно CP для апгрейда (нужно {cost}, есть {farm.cp})")

    farm.cp -= cost
    setattr(farm, level_attr, level + 1)

    await session.commit()
    return _farm_state(farm)


async def upgrade_tap(session: AsyncSession, chat_id: int, user_id: int) -> dict:
    """D-03: апгрейд тапа — cost = int(round(TAP_UPGRADE_BASE*1.15**tap_level))."""
    return await _upgrade(session, chat_id, user_id, TAP_UPGRADE_BASE, "tap_level")


async def upgrade_auto(session: AsyncSession, chat_id: int, user_id: int) -> dict:
    """D-03: апгрейд автокликера — cost = int(round(AUTO_UPGRADE_BASE*1.15**auto_level))."""
    return await _upgrade(session, chat_id, user_id, AUTO_UPGRADE_BASE, "auto_level")


# --- AMM CP<->ювик: pool get-or-create, quote, convert, buy -----------------


async def _get_or_create_pool(session: AsyncSession, chat_id: int) -> ClickerMarketPool:
    """Идемпотентный get-or-create строки AMM-пула (форма `chat_bank`/
    `_get_or_create_farm`): `pg_insert(...).on_conflict_do_nothing` с
    семенными резервами (`AMM_SEED_R_CP`/`AMM_SEED_R_H` — их отношение равно
    якорю), затем безусловный `SELECT ... FOR UPDATE` — лочим пул ПЕРВЫМ,
    до любого движения ювиков (лок-ординг, T-04.1-15)."""
    stmt = (
        pg_insert(ClickerMarketPool)
        .values(chat_id=chat_id, r_cp=Decimal(AMM_SEED_R_CP), r_h=Decimal(AMM_SEED_R_H))
        .on_conflict_do_nothing(index_elements=["chat_id"])
    )
    await session.execute(stmt)

    pool = (
        await session.execute(
            select(ClickerMarketPool).where(ClickerMarketPool.chat_id == chat_id).with_for_update()
        )
    ).scalar_one()
    return pool


def _pool_price(pool: ClickerMarketPool) -> Decimal:
    """Курс пула — CP за 1 ювик (тот же масштаб, что якорь
    `AMM_ANCHOR_CP_PER_HRYVNA`)."""
    return pool.r_cp / pool.r_h


def quote_convert(pool: ClickerMarketPool, cp_in: int) -> tuple[int, Decimal, Decimal]:
    """Constant-product котировка продажи `cp_in` CP в пул (convert):
    `k = r_cp*r_h`; ював на выходе = `r_h - k/(r_cp+cp_in)`, floor до int
    (T-04.1-16 — пул никогда не платит больше, чем позволяет кривая; остаток
    floor-округления остаётся в резерве `r_h`, а не теряется бесследно).
    Слиппедж встроен в саму кривую — крупный `cp_in` даёт худший курс за
    единицу CP. Возвращает `(hryvnia_out, new_r_cp, new_r_h)`, ничего не
    мутирует — чистая функция над переданным объектом пула."""
    if cp_in <= 0:
        raise ClickerError("cp_in должен быть положительным")

    k = pool.r_cp * pool.r_h
    new_r_cp = pool.r_cp + Decimal(cp_in)
    hryvnia_out_exact = pool.r_h - (k / new_r_cp)
    hryvnia_out = max(0, int(hryvnia_out_exact))
    new_r_h = pool.r_h - Decimal(hryvnia_out)
    return hryvnia_out, new_r_cp, new_r_h


def quote_buy(pool: ClickerMarketPool, hryvnia_in: int) -> tuple[int, Decimal, Decimal]:
    """Constant-product котировка покупки CP за `hryvnia_in` ювиков (buy):
    `k = r_cp*r_h`; CP на выходе = `r_cp - k/(r_h+hryvnia_in)`, floor до int
    (та же T-04.1-16 гарантия, что у `quote_convert`). Возвращает
    `(cp_out, new_r_cp, new_r_h)`, ничего не мутирует."""
    if hryvnia_in <= 0:
        raise ClickerError("hryvnia_in должен быть положительным")

    k = pool.r_cp * pool.r_h
    new_r_h = pool.r_h + Decimal(hryvnia_in)
    cp_out_exact = pool.r_cp - (k / new_r_h)
    cp_out = max(0, int(cp_out_exact))
    new_r_cp = pool.r_cp - Decimal(cp_out)
    return cp_out, new_r_cp, new_r_h


async def convert_cp(
    session: AsyncSession, chat_id: int, user_id: int, cp_in: int, ref_id: str
) -> dict:
    """Продажа CP фермы в ювики через AMM (mint — санкционированный источник
    эмиссии, как и "продажа cp через AMM" в REFERENCE-XYLOZ.md §3.1).

    Лок-ординг (T-04.1-15): пул — `FOR UPDATE` ПЕРВЫМ, затем ферма. Котировка
    считается ДО любой мутации. `economy_service.credit` вызывается ПЕРЕД
    мутацией `farm.cp`/резервов пула (Rule 1 — отклонение от буквального
    текста плана 04.1-05, где мутация шла до credit): так повтор с тем же
    `ref_id` — истинный no-op не только для ювиков (что и требовал план), но
    и для внутреннего ресурса CP фермы (иначе повтор молча сжигал бы CP
    второй раз без компенсации) — та же идиома "debit/credit-then-mutate",
    что уже используется в `buy_cp`/`markets_service.place_bet`.

    Поднимает `ClickerError`, если `farm.cp < cp_in`. Возвращает
    `{"cp_in", "hryvnia_out", "price"}` при успехе, либо
    `{"status": "duplicate", "hryvnia_out": 0}` на повторный `ref_id`.
    """
    pool = await _get_or_create_pool(session, chat_id)
    farm = await _get_or_create_farm(session, chat_id, user_id)
    await _accrue_offline(session, chat_id, user_id, farm)

    if farm.cp < cp_in:
        raise ClickerError(f"Недостаточно CP для конвертации (нужно {cp_in}, есть {farm.cp})")

    hryvnia_out, new_r_cp, new_r_h = quote_convert(pool, cp_in)

    credited = await economy_service.credit(
        session, chat_id, user_id, hryvnia_out, kind="farm_convert", ref_id=ref_id
    )
    if not credited:
        logger.info("convert_cp: ref_id=%s уже обработан, пропускаем", ref_id)
        await session.commit()
        return {"status": "duplicate", "hryvnia_out": 0}

    farm.cp -= cp_in
    pool.r_cp = new_r_cp
    pool.r_h = new_r_h
    price = _pool_price(pool)
    session.add(ClickerMarketPrice(chat_id=chat_id, price=price))

    await session.commit()
    return {"cp_in": cp_in, "hryvnia_out": hryvnia_out, "price": price}


async def buy_cp(
    session: AsyncSession, chat_id: int, user_id: int, hryvnia_in: int, ref_id: str
) -> dict:
    """Покупка CP за ювики через AMM (ював — sink в банк чата, как и остальные
    ставки/комиссии в проекте).

    Лок-ординг: пул — `FOR UPDATE` ПЕРВЫМ. `economy_service.debit` вызывается
    ПЕРВЫМ (до мутации `farm.cp`/резервов) — та же "debit-then-mutate"
    идиома, что и `markets_service.place_bet`: повтор с тем же `ref_id` —
    no-op, ни ювики, ни CP фермы, ни резервы пула не двигаются повторно.

    Поднимает `economy_service.InsufficientFunds`, если баланса не хватает.
    Возвращает `{"hryvnia_in", "cp_out", "price"}` при успехе, либо
    `{"status": "duplicate", "cp_out": 0}` на повторный `ref_id`.
    """
    pool = await _get_or_create_pool(session, chat_id)
    farm = await _get_or_create_farm(session, chat_id, user_id)
    await _accrue_offline(session, chat_id, user_id, farm)

    debited = await economy_service.debit(
        session, chat_id, user_id, hryvnia_in, kind="farm_buy_cp", ref_id=ref_id
    )
    if not debited:
        logger.info("buy_cp: ref_id=%s уже обработан, пропускаем", ref_id)
        await session.commit()
        return {"status": "duplicate", "cp_out": 0}

    await economy_service.credit_bank(
        session, chat_id, hryvnia_in, kind="farm_buy_cp", ref_id=f"{ref_id}:bank"
    )

    cp_out, new_r_cp, new_r_h = quote_buy(pool, hryvnia_in)
    farm.cp += cp_out
    pool.r_cp = new_r_cp
    pool.r_h = new_r_h
    price = _pool_price(pool)
    session.add(ClickerMarketPrice(chat_id=chat_id, price=price))

    await session.commit()
    return {"hryvnia_in": hryvnia_in, "cp_out": cp_out, "price": price}


async def get_market_state(
    session: AsyncSession,
    chat_id: int,
    history_limit: int = 200,
    quote_amounts: list[int] | None = None,
) -> dict:
    """Read-путь AMM-рынка (get-or-create пула + текущий курс + ограниченная
    история котировок — до `history_limit` последних снапшотов
    `ClickerMarketPrice`, по умолчанию 200). Самодостаточная read-операция —
    коммитит сама (форма `economy_service.get_balance`).

    `quote_amounts` (Claude's discretion, withdraw-UX план 2026-07-23):
    опциональный список сумм CP — для каждой тем же залоченным снапшотом
    пула считается `quote_convert`-превью БЕЗ исполнения свопа (чистая
    функция, резервы пула не мутируются). Даёт клиенту таблицу деградации
    курса (price impact) и живой "получишь ≈ X" ДО подтверждения обмена, не
    открывая отдельный мутирующий эндпоинт. `effective_price`/`impact` тут
    же не считаются — фронтенд выводит их из `price` + `hryvnia_out`, тот же
    паттерн "клиентское зеркало формулы", что уже используют
    tap_level/upgrade-cost на экране фермы."""
    pool = await _get_or_create_pool(session, chat_id)
    price = _pool_price(pool)

    history_rows = (
        await session.execute(
            select(ClickerMarketPrice)
            .where(ClickerMarketPrice.chat_id == chat_id)
            .order_by(ClickerMarketPrice.created_at.desc())
            .limit(history_limit)
        )
    ).scalars().all()

    quotes = []
    for amount in quote_amounts or []:
        if amount <= 0:
            continue
        hryvnia_out, _new_r_cp, _new_r_h = quote_convert(pool, amount)
        quotes.append({"cp_in": amount, "hryvnia_out": hryvnia_out})

    await session.commit()
    return {
        "price": price,
        "r_cp": pool.r_cp,
        "r_h": pool.r_h,
        "history": [
            {"price": row.price, "created_at": row.created_at} for row in reversed(history_rows)
        ],
        "quotes": quotes,
    }


# --- amm_tick (mean-reversion, D-03) + APScheduler --------------------------


async def amm_tick(session: AsyncSession) -> int:
    """Mean-reversion тик (D-03): для КАЖДОГО ряда `ClickerMarketPool`
    (per-row try/except — одна упавшая строка не должна ронять весь батч,
    форма `markets_service.auto_resolve_external`) тянет резервы к якорю
    (`AMM_ANCHOR_CP_PER_HRYVNA`) множителем `factor = exp(-TICK/TAU)`
    (`_MEAN_REVERSION_FACTOR`, считается один раз при импорте модуля):
    целевые резервы (`target_r_h = sqrt(k/anchor)`, `target_r_cp =
    target_r_h*anchor`) сохраняют текущий `k` пула, новые резервы —
    взвешенное среднее `current*factor + target*(1-factor)`. Пишет снапшот
    новой цены (`ClickerMarketPrice`) для каждого тронутого пула. Пул
    блокируется `FOR UPDATE` — сериализуется с конкурентными свопами
    (T-04.1-15). Возвращает число реально тронутых пулов."""
    pools = (
        await session.execute(select(ClickerMarketPool).with_for_update())
    ).scalars().all()

    anchor = Decimal(AMM_ANCHOR_CP_PER_HRYVNA)
    ticked = 0
    for pool in pools:
        try:
            k = pool.r_cp * pool.r_h
            target_r_h = (k / anchor).sqrt()
            target_r_cp = target_r_h * anchor

            new_r_cp = pool.r_cp * _MEAN_REVERSION_FACTOR + target_r_cp * (1 - _MEAN_REVERSION_FACTOR)
            new_r_h = pool.r_h * _MEAN_REVERSION_FACTOR + target_r_h * (1 - _MEAN_REVERSION_FACTOR)

            pool.r_cp = new_r_cp
            pool.r_h = new_r_h
            session.add(ClickerMarketPrice(chat_id=pool.chat_id, price=new_r_cp / new_r_h))
            ticked += 1
        except Exception:  # noqa: BLE001 - тик обязан пережить любую ошибку по одному пулу
            logger.exception("amm_tick: тик упал для chat_id=%s", pool.chat_id)

    await session.commit()
    return ticked


_AMM_TICK_JOB_ID = "amm_mean_reversion"


def register_amm_tick(scheduler: AsyncIOScheduler) -> None:
    """Регистрирует фоновый mean-reversion тик как interval-job (~10 минут,
    `AMM_TICK_MINUTES`), по образцу `markets_service.register_auto_close`:
    своя сессия, broad-except — тик обязан пережить любую ошибку и не
    уронить планировщик."""

    async def _job() -> None:
        async with SessionLocal() as session:
            try:
                ticked = await amm_tick(session)
                if ticked:
                    logger.info("amm_mean_reversion: тик применён к пулам — %s", ticked)
            except Exception:  # noqa: BLE001 - job обязан пережить любую ошибку и не уронить планировщик
                logger.exception("amm_mean_reversion: тик упал")

    scheduler.add_job(
        _job,
        "interval",
        minutes=AMM_TICK_MINUTES,
        coalesce=True,
        max_instances=1,
        misfire_grace_time=300,
        id=_AMM_TICK_JOB_ID,
        replace_existing=True,
    )
