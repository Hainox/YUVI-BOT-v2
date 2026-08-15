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
  — `accepted = min(count, int(MAX_CPS*elapsed_ms/1000))`. Раньше был пол
  `max(1, ...)`, эксплуатируемый шквалом запросов (см. докстринг `tap()`) —
  убран; `last_tap_at` продвигается только при `accepted > 0`.
- Оффлайн-накопление автокликера (T-04.1-13): считается НА КАЖДОМ обращении
  (`_accrue_offline`), а не фоновым тиком на юзера — `elapsed` берётся из
  разницы `now - last_accrued_at` (серверных значений), клиент не может
  подделать elapsed для начисления; капается `MAX_OFFLINE_SECONDS` (4ч).
- Стоимость апгрейда (T-04.1-14): `int(round(base * UPGRADE_GROWTH**level))`,
  считается сервером, при нехватке CP апгрейд отклоняется `ClickerError`.
- Идемпотентность апгрейдов (bugfix аудита 2026-08-05): `upgrade_tap`/
  `upgrade_auto`/`upgrade_character` берут обязательный `ref_id` и клеймят
  его в `ClickerUpgradeLog` (`_claim_upgrade_ref`) ДО мутации CP/уровня —
  credit-first-then-mutate идиома, как у `convert_cp`/`buy_cp` ниже, но своя
  таблица вместо `economy_tx` (эти три пути тратят исключительно внутренний
  CP, ювики не двигают, см. абзац выше). Сетевой ретрай с тем же `ref_id` —
  истинный no-op: `{"status": "duplicate", ...}`, ни CP, ни уровень повторно
  не двигаются.

AMM CP<->ювик (D-03, REFERENCE-XYLOZ.md §3.1 `market_service.py`) — per-чат
constant-product пул (`ClickerMarketPool.r_cp * r_h = k`), слиппедж встроен
в саму кривую, плюс фоновый mean-reversion тик (~10 мин) тянет цену к якорю
(100 CP/ювик) и пишет снапшот (`ClickerMarketPrice`) для графика. Резервы и
цена — `Decimal`/Numeric(20,8), НИКОГДА float (CR-03: плавающая точка
накапливает погрешность округления при повторных умножениях/делениях
constant-product). Пул блокируется `SELECT ... FOR UPDATE` до любой мутации
резервов (T-04.1-15) — свопы и тик сериализуются на строке пула.

`amm_tick` (bugfix аудита 2026-08-05): интерполяция резервов идёт В
LOG-PRICE-ПРОСТРАНСТВЕ с реконструкцией резервов при ФИКСИРОВАННОМ текущем
k, а НЕ прямой линейной интерполяцией резервов — прямая интерполяция между
двумя точками одной гиперболы `r_cp*r_h=k` лежит строго НАД гиперболой
(выпуклость), поэтому раньше k монотонно рос при каждом тике, пока цена не
на якоре, в ЛЮБУЮ сторону, без естественного затухания (см. докстринг
`amm_tick`). Новая формула держит k тика тождественно постоянным (с точностью
до Decimal-погрешности округления) при любом `_MEAN_REVERSION_FACTOR`.
"""

from __future__ import annotations

import logging
import math
from datetime import datetime
from decimal import Decimal

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import insert
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import settings
from bot.services import achievements_service
from bot.services import constellation_catalog
from bot.services import economy_service
from bot.services import gacha_catalog
from bot.services import quests_service
from common.db.session import SessionLocal
from common.models.clicker_farm import ClickerFarm
from common.models.clicker_market_pool import ClickerMarketPool
from common.models.clicker_market_price import ClickerMarketPrice
from common.models.clicker_upgrade_log import ClickerUpgradeLog
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

# --- GACHA-02: доход фермы от собранных героинь ------------------------------
#
# Раньше ставки были портированы с REFERENCE-XYLOZ.md §3.1 (5 именных
# РАБОТНИЦ) на тиры каталога с фильтром role=="worker" (только часть тира
# участвовала в доходе). После того как ростер вырос с 10 плейсхолдеров до
# 15 героинь Ювитерии и фильтр role снят (ЛЮБАЯ собранная героиня даёт
# доход — см. `gacha_catalog.py`), ставки пересчитаны, чтобы суммарный
# потенциал тира не удвоился незаметно вместе со снятием фильтра: старый
# "бюджет тира" (прежняя ставка × число прежних worker-персонажей) поделён
# на новое, большее число героинь тира:
# R 0.2/5=0.04, S 1.0/4=0.25, UR 1.5/3=0.50, UUR 5.0/3≈1.67.
WORKER_TIER_CP_PER_SEC: dict[str, float] = {
    "R": 0.04,
    "S": 0.25,
    "UR": 0.50,
    "UUR": 1.67,
}

# --- GACHA-05: прокачка отдельной героини в ферме (idle-game-стиль) ---------
#
# Отдельно от constellation-уровня (растёт от ДУБЛЕЙ, sum_effect выше) —
# здесь игрок тратит CP фермы, чтобы поднять farm_level КОНКРЕТНОЙ героини
# (стартует с 1 при получении, как tap_level стартует с 1). Каждый уровень
# прибавляет FARM_LEVEL_RATE_PER_TIER[tier] CP/сек героини ДО star_mult/
# constellation-бонуса (см. _collection_income_per_sec) — тот же порядок
# множителей, что уже применяется к WORKER_TIER_CP_PER_SEC. Кост растёт по
# уже проверенной кривой _upgrade_cost (base*1.15**level, как тап/автокликер).
FARM_LEVEL_MAX = 50  # как MAX_WORKER_LEVEL в эталоне REFERENCE-XYLOZ.md §3.1

FARM_LEVEL_RATE_PER_TIER: dict[str, float] = {
    "R": 0.004,
    "S": 0.025,
    "UR": 0.05,
    "UUR": 0.167,
}

FARM_LEVEL_BASE_COST: dict[str, int] = {
    "R": 50,
    "S": 400,
    "UR": 2000,
    "UUR": 8000,
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
    """GACHA-02/GACHA-04/GACHA-05: сумма CP/сек от ВСЕХ собранных героинь -
    (WORKER_TIER_CP_PER_SEC[tier] + FARM_LEVEL_RATE_PER_TIER[tier]*(farm_level-1))
    * gacha_catalog.star_mult(stars) по каждой строке gacha_collection (роль
    персонажа больше не фильтрует доход, см. gacha_catalog.py), ДОПОЛНИТЕЛЬНО
    умноженная на (1 + constellation-бонус FARM_CP_PCT этой героини на её
    текущем const_level) — единственный constellation-эффект, реально
    подключённый к игровой логике (constellation_catalog.py хранит остальные
    8 категорий бонусов как готовые данные, но НЕ применяет их нигде ещё, см.
    докстринг constellation_catalog.py). farm_level растёт ЗА CP через
    upgrade_character (GACHA-05) — отдельно от const_level, который растёт от
    дублей, не от траты валюты."""
    rows = (
        await session.execute(
            select(
                GachaCollection.char_id,
                GachaCollection.stars,
                GachaCollection.copies,
                GachaCollection.farm_level,
            ).where(GachaCollection.chat_id == chat_id, GachaCollection.user_id == user_id)
        )
    ).all()
    total = 0.0
    for char_id, stars, copies, farm_level in rows:
        char = gacha_catalog.CATALOG.get(char_id)
        if char is None:
            continue
        level_bonus = FARM_LEVEL_RATE_PER_TIER[char.tier] * (farm_level - 1)
        base = (WORKER_TIER_CP_PER_SEC[char.tier] + level_bonus) * gacha_catalog.star_mult(stars)
        level = constellation_catalog.const_level(copies)
        bonus_pct = constellation_catalog.sum_effect(char_id, level, constellation_catalog.FARM_CP_PCT)
        total += base * (1 + bonus_pct)
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


def _farm_state(
    farm: ClickerFarm, accepted: int | None = None, effective_max_level: int | None = None
) -> dict:
    state = {
        "cp": farm.cp,
        "tap_level": farm.tap_level,
        "auto_level": farm.auto_level,
    }
    if accepted is not None:
        state["accepted"] = accepted
    if effective_max_level is not None:
        state["effective_max_level"] = effective_max_level
        state["base_max_level"] = settings.farm_max_level
    return state


async def get_farm_state(session: AsyncSession, chat_id: int, user_id: int) -> dict:
    """Read-путь, который поллит Mini App: get-or-create + оффлайн-накопление
    + коммит + текущее состояние фермы (cp/tap_level/auto_level/cp_per_sec —
    последнее включает GACHA-02 доход коллекции, отображается фронтендом)."""
    farm = await _get_or_create_farm(session, chat_id, user_id)
    worker_cp_per_sec = await _accrue_offline(session, chat_id, user_id, farm)
    effective_max_level = await get_effective_max_level(session, chat_id, user_id)
    await session.commit()
    state = _farm_state(farm, effective_max_level=effective_max_level)
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
    """Анти-чит тап (D-03/T-04.1-12): `accepted = min(count,
    int(MAX_CPS*elapsed_ms/1000))` — клиентский `count` никогда не
    принимается напрямую. `elapsed_ms` тоже не принимается напрямую (CR-02):
    клэмпится сверху реальным серверным интервалом с прошлого принятого тапа
    (`last_tap_at`, пишется ТОЛЬКО этой функцией — в отличие от
    `last_accrued_at`, который сбрасывает каждый poll `get_farm_state`, что
    сделало бы его непригодным для анти-чита тапа). CP растёт на
    `accepted * tap_value(tap_level)`.

    `last_tap_at` продвигается ТОЛЬКО когда `accepted > 0` (не на каждый
    вызов): раньше здесь был пол `max(1, ...)`, гарантировавший минимум один
    принятый тап на КАЖДЫЙ запрос независимо от реального интервала — при
    этом `last_tap_at` всё равно сбрасывался в `now`, так что шквал запросов
    чаще ~33мс (1000/MAX_CPS) друг за другом каждый раз проходил через этот
    пол и накручивал CP пропорционально числу запросов, а не прошедшему
    времени. Без пола и с условным продвижением часов накопленное-но-ещё-
    недостаточное время не сбрасывается впустую: запрос, которому не хватает
    времени на хотя бы 1 тап, получает `accepted=0` и оставляет `last_tap_at`
    нетронутым, так что реальное время продолжает копиться до следующего
    вызова — легитимный тап после паузы по-прежнему засчитывается, а серия
    запросов чаще MAX_CPS/сек — нет, сколько бы их ни прислали."""
    farm = await _get_or_create_farm(session, chat_id, user_id)
    await _accrue_offline(session, chat_id, user_id, farm)

    now = datetime.utcnow()
    server_elapsed_ms = max(0.0, (now - farm.last_tap_at).total_seconds() * 1000)
    trusted_elapsed_ms = min(elapsed_ms, server_elapsed_ms)

    accepted = min(count, int(MAX_CPS * trusted_elapsed_ms / 1000))
    if accepted > 0:
        farm.last_tap_at = now
        farm.cp += accepted * tap_value(farm.tap_level)

    await session.commit()
    return _farm_state(farm, accepted=accepted)


# --- QUEST-01/02: эффективный потолок фермы (квесты/ачивки поднимают до 99) --

FARM_EFFECTIVE_CAP_CEILING = 99  # жёсткий потолок вне зависимости от баланса каталогов квестов/ачивок
QUEST_COMPLETIONS_PER_BONUS_LEVEL = 3  # каждые N выполненных квестов (всего, любой день/ключ) = +1 уровень


async def get_effective_max_level(session: AsyncSession, chat_id: int, user_id: int) -> int:
    """Эффективный потолок tap_level/auto_level ЭТОГО участника (QUEST-01/02):
    settings.farm_max_level + бонус от квестов (total_completions //
    QUEST_COMPLETIONS_PER_BONUS_LEVEL) + сумма bonus_levels разблокированных
    ачивок, зажатые FARM_EFFECTIVE_CAP_CEILING.

    Считается ЖИВЬЁМ из quest_completions/achievement_unlocks (не кэшируется
    отдельной колонкой на ClickerFarm) — обе таблицы уникально индексированы
    по (chat_id, user_id, ...), COUNT/SUM дешёвые индексные запросы, а живой
    расчёт не требует инвалидации кэша при каждом новом квесте/ачивке (та же
    философия, что awards_service: считать из источника, не кэшировать
    производное). Вызывается только на путях апгрейда/дисплея фермы, не на
    hot path tap()."""
    total_completions = await quests_service.get_total_completions(session, chat_id, user_id)
    achievement_bonus = await achievements_service.get_total_bonus_levels(session, chat_id, user_id)
    quest_bonus = total_completions // QUEST_COMPLETIONS_PER_BONUS_LEVEL
    return min(FARM_EFFECTIVE_CAP_CEILING, settings.farm_max_level + quest_bonus + achievement_bonus)


async def _claim_upgrade_ref(
    session: AsyncSession, chat_id: int, user_id: int, kind: str, ref_id: str
) -> bool:
    """Идемпотентный "клейм" `ref_id` для CP-only апгрейда (bugfix аудита
    2026-08-05) — та же SAVEPOINT + UNIQUE + IntegrityError-on-replay идиома,
    что `economy_service.credit`/`debit` (`_log_tx` внутри `begin_nested()`),
    но в собственной таблице `ClickerUpgradeLog`, а не `economy_tx`: эти
    апгрейды тратят исключительно внутренний CP фермы, ювики не двигают (см.
    докстринг модуля) — писать в `economy_tx` было бы смешением денежного
    журнала с чисто внутренней CP-бухгалтерией.

    Возвращает `True`, если `ref_id` заклеймлен впервые (вызывающий обязан
    СРАЗУ ПОСЛЕ этого применить мутацию CP/уровня — credit-first-then-mutate,
    как в `convert_cp`/`buy_cp`), `False` — если `(chat_id, ref_id, kind)` уже
    встречался (повтор/сетевой ретрай) — мутация ПРОПУСКАЕТСЯ вызывающим.
    Не коммитит — транзакцию завершает вызывающий."""
    try:
        async with session.begin_nested():
            await session.execute(
                insert(ClickerUpgradeLog).values(
                    chat_id=chat_id, user_id=user_id, kind=kind, ref_id=ref_id
                )
            )
    except IntegrityError:
        logger.info(
            "_claim_upgrade_ref: ref_id=%s (kind=%s) уже применён, пропускаем", ref_id, kind
        )
        return False
    return True


async def _upgrade(
    session: AsyncSession,
    chat_id: int,
    user_id: int,
    base: int,
    level_attr: str,
    kind: str,
    ref_id: str,
) -> dict:
    """Общее ядро апгрейда тапа/автокликера (T-04.1-14): cost считается ДО
    списания, при нехватке CP апгрейд отклоняется без изменения состояния.

    Потолок уровня проверяется первым, до расчёта cost — апгрейд выше
    потолка отклоняется без изменения состояния, как и нехватка CP. Потолок
    считается через `get_effective_max_level` (QUEST-01/02): база
    `settings.farm_max_level` плюс бонусы от выполненных квестов и
    разблокированных ачивок, зажатые `FARM_EFFECTIVE_CAP_CEILING`=99; при
    нулевых разблокировках это эквивалентно старой плоской проверке
    `settings.farm_max_level`.

    Идемпотентность (bugfix аудита 2026-08-05): `ref_id` клеймится
    (`_claim_upgrade_ref`) ПОСЛЕ валидации (потолок/cost), но ДО мутации
    CP/уровня — повтор с тем же `ref_id` не двигает CP и не поднимает
    уровень повторно, возвращает `{"status": "duplicate", ...}` текущим
    (не изменённым) состоянием фермы."""
    farm = await _get_or_create_farm(session, chat_id, user_id)
    await _accrue_offline(session, chat_id, user_id, farm)

    effective_cap = await get_effective_max_level(session, chat_id, user_id)
    level = getattr(farm, level_attr)
    if level >= effective_cap:
        raise ClickerError(f"Достигнут максимальный уровень ({effective_cap})")

    cost = _upgrade_cost(base, level)
    if farm.cp < cost:
        raise ClickerError(f"Недостаточно CP для апгрейда (нужно {cost}, есть {farm.cp})")

    claimed = await _claim_upgrade_ref(session, chat_id, user_id, kind=kind, ref_id=ref_id)
    if not claimed:
        await session.commit()
        return {**_farm_state(farm), "status": "duplicate"}

    farm.cp -= cost
    setattr(farm, level_attr, level + 1)

    await session.commit()
    return _farm_state(farm)


async def upgrade_tap(session: AsyncSession, chat_id: int, user_id: int, ref_id: str) -> dict:
    """D-03: апгрейд тапа — cost = int(round(TAP_UPGRADE_BASE*1.15**tap_level))."""
    return await _upgrade(
        session, chat_id, user_id, TAP_UPGRADE_BASE, "tap_level", kind="tap", ref_id=ref_id
    )


async def upgrade_auto(session: AsyncSession, chat_id: int, user_id: int, ref_id: str) -> dict:
    """D-03: апгрейд автокликера — cost = int(round(AUTO_UPGRADE_BASE*1.15**auto_level))."""
    return await _upgrade(
        session, chat_id, user_id, AUTO_UPGRADE_BASE, "auto_level", kind="auto", ref_id=ref_id
    )


async def _get_collection_row_for_update(
    session: AsyncSession, chat_id: int, user_id: int, char_id: str
) -> GachaCollection | None:
    return (
        await session.execute(
            select(GachaCollection)
            .where(
                GachaCollection.chat_id == chat_id,
                GachaCollection.user_id == user_id,
                GachaCollection.char_id == char_id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()


async def upgrade_character(
    session: AsyncSession, chat_id: int, user_id: int, char_id: str, ref_id: str
) -> dict:
    """GACHA-05: индивидуальная прокачка ОДНОЙ героини фермы (idle-game-стиль,
    отдельно от constellation-уровня, который растёт от дублей, а не от
    траты валюты). Стоит CP фермы — тот же кошелёк и та же кривая стоимости
    (`_upgrade_cost`, base*1.15**level), что тап/автокликер, кап
    `FARM_LEVEL_MAX`=50 (как MAX_WORKER_LEVEL в эталоне). Порядок блокировок
    — строка фермы ПЕРВОЙ, затем строка `gacha_collection` героини (тот же
    контракт, что `gacha_service.roll`, см. его докстринг).

    Идемпотентность (bugfix аудита 2026-08-05) — та же credit-first-then-
    mutate идиома, что у `_upgrade`/`convert_cp`/`buy_cp`: `ref_id` клеймится
    ПОСЛЕ валидации (собрана/потолок/cost), но ДО мутации CP/farm_level.
    Повтор с тем же `ref_id` — `{"status": "duplicate", ...}`, без повторного
    списания CP/подъёма уровня."""
    char = gacha_catalog.CATALOG.get(char_id)
    if char is None:
        raise ClickerError("Неизвестный персонаж")

    farm = await _get_or_create_farm(session, chat_id, user_id)
    await _accrue_offline(session, chat_id, user_id, farm)

    row = await _get_collection_row_for_update(session, chat_id, user_id, char_id)
    if row is None:
        raise ClickerError("Героиня ещё не собрана")

    if row.farm_level >= FARM_LEVEL_MAX:
        raise ClickerError(f"Достигнут максимальный уровень ({FARM_LEVEL_MAX})")

    cost = _upgrade_cost(FARM_LEVEL_BASE_COST[char.tier], row.farm_level)
    if farm.cp < cost:
        raise ClickerError(f"Недостаточно CP для апгрейда (нужно {cost}, есть {farm.cp})")

    claimed = await _claim_upgrade_ref(session, chat_id, user_id, kind="character", ref_id=ref_id)
    if not claimed:
        await session.commit()
        state = {**_farm_state(farm), "status": "duplicate"}
        state["char_id"] = char_id
        state["farm_level"] = row.farm_level
        return state

    farm.cp -= cost
    row.farm_level += 1

    await session.commit()
    state = _farm_state(farm)
    state["char_id"] = char_id
    state["farm_level"] = row.farm_level
    return state


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
    форма `markets_service.auto_resolve_external`) тянет ЦЕНУ к якорю
    (`AMM_ANCHOR_CP_PER_HRYVNA`) множителем `factor = exp(-TICK/TAU)`
    (`_MEAN_REVERSION_FACTOR`, считается один раз при импорте модуля).

    bugfix аудита 2026-08-05 (было — HIGH, монотонная инфляция k): интерполяция
    идёт в LOG-PRICE-пространстве (`current_price = r_cp/r_h`, тот же
    масштаб/ориентация, что `_pool_price`), а не прямой линейной
    интерполяцией резервов — прямая линия между двумя точками одной
    гиперболы `r_cp*r_h=k` лежит строго НАД гиперболой (выпуклость), поэтому
    старая формула монотонно растила k на каждом тике, пока цена не на
    якоре, в любую сторону, без затухания. Новые резервы реконструируются из
    интерполированной цены при ТЕКУЩЕМ (до тика) k, зафиксированном — тик
    двигает цену, но НИКОГДА не меняет k (с точностью до Decimal-погрешности
    округления):
        new_price = exp(factor*ln(current_price) + (1-factor)*ln(anchor))
        new_r_cp = sqrt(k * new_price); new_r_h = k / new_r_cp
    Пишет снапшот новой цены (`ClickerMarketPrice`) для каждого тронутого
    пула. Пул блокируется `FOR UPDATE` — сериализуется с конкурентными
    свопами (T-04.1-15). Возвращает число реально тронутых пулов."""
    pools = (
        await session.execute(select(ClickerMarketPool).with_for_update())
    ).scalars().all()

    anchor = Decimal(AMM_ANCHOR_CP_PER_HRYVNA)
    ln_anchor = anchor.ln()
    ticked = 0
    for pool in pools:
        try:
            k = pool.r_cp * pool.r_h
            current_price = pool.r_cp / pool.r_h

            log_new_price = (
                _MEAN_REVERSION_FACTOR * current_price.ln() + (1 - _MEAN_REVERSION_FACTOR) * ln_anchor
            )
            new_price = log_new_price.exp()

            new_r_cp = (k * new_price).sqrt()
            new_r_h = k / new_r_cp

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
