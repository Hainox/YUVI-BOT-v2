"""Ролл гачи (GACHA-01..03 бэкенд, D-03/D-07) — деньги двигает ТОЛЬКО через
`bot.services.economy_service` (`debit`+`credit_bank` для стоимости ролла —
сток в банк чата; `credit` — refund дублей сверх 5★, минт). Этот модуль
НИКОГДА не пишет `user_balance`/`chat_bank`/`economy_tx` напрямую
(`economy_service.py` — единственный модуль с таким правом, см. его
докстринг).

Каталог персонажей — полностью в коде (`gacha_catalog.py`), не в БД. Pity
(`pity_ssr`/`pity_ur`) живёт на строке `clicker_farms` (общая с
`clicker_service.py`) — переиспользуем его `_get_or_create_farm`, а не
дублируем ту же upsert+`FOR UPDATE` логику (04.1-PATTERNS Pattern 7).

Идемпотентность: стоимость ролла списывается через `economy_service.debit`
с вызывающим `ref_id` — повтор того же `ref_id` не двигает деньги повторно
(no-op, `replay: True` в результате, гранты не создаются). Грант в
`GachaCollection` защищён UNIQUE(user_id, chat_id, char_id) + race-safe
SAVEPOINT-рестарт на гонку с конкурентной сессией (04.1-PATTERNS "race-safe
partial-UNIQUE + rollback", форма `markets_service.import_market`).

Контракт порядка блокировок: строка фермы (`clicker_farms`, через
`clicker_service._get_or_create_farm`) блокируется ПЕРВОЙ (нужна для pity),
затем по одной строке `gacha_collection` на грант.

Все исходы — ТОЛЬКО через `secrets.SystemRandom()` (модульный RNG-сим
`_rng`, подменяется в тестах monkeypatch'ем, форма `casino_service._rng`) —
server-authoritative; R-тир НЕДОСТИЖИМ через ролл (D-07 — `gacha_catalog.
TIER_WEIGHTS` не содержит ключ "R", не добавлять его самостоятельно).
"""

from __future__ import annotations

import logging
import secrets

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from bot.services import clicker_service
from bot.services import economy_service
from bot.services import gacha_catalog
from bot.services import settings_service
from common.models.clicker_farm import ClickerFarm
from common.models.gacha_collection import GachaCollection

logger = logging.getLogger(__name__)

_rng = secrets.SystemRandom()

ROLL_COST = 300  # D-03
ROLL10_COST = 2700  # D-03: ×10 = ×9 (скидка)
RATE_UP_WEIGHT = 0.5  # D-03: вес rate-up баннера среди UR
GACHA_BANNER_KEY = "gacha_banner"  # ключ BotSetting (rate-up баннер)

_SR_OR_BETTER = frozenset({"SR", "SSR", "UR"})


class GachaError(Exception):
    """Базовое исключение гача-модуля."""


# --- Выбор тира с pity (D-03) -------------------------------------------------


def _weighted_choice(weights: dict[str, float]) -> str:
    """Взвешенный выбор ключа словаря через модульный `_rng.random()`
    (подменяемый в тестах, форма `casino_service._rng`)."""
    total = sum(weights.values())
    point = _rng.random() * total
    cumulative = 0.0
    for tier, weight in weights.items():
        cumulative += weight
        if point < cumulative:
            return tier
    return next(reversed(weights))  # защита от погрешности float на границе суммы


def _pick_tier(farm: ClickerFarm) -> str:
    """D-03: pity применяется ПЕРВЫМ — pity_ur форсирует UR (порог PITY_UR),
    иначе pity_ssr форсирует SSR-or-better (порог PITY_SSR), иначе честный
    взвешенный выбор по `gacha_catalog.TIER_WEIGHTS` (R НЕДОСТИЖИМ, D-07 —
    в весах нет ключа "R")."""
    if farm.pity_ur + 1 >= gacha_catalog.PITY_UR:
        return "UR"
    if farm.pity_ssr + 1 >= gacha_catalog.PITY_SSR:
        return _weighted_choice(
            {"SSR": gacha_catalog.TIER_WEIGHTS["SSR"], "UR": gacha_catalog.TIER_WEIGHTS["UR"]}
        )
    return _weighted_choice(gacha_catalog.TIER_WEIGHTS)


def _apply_pity(farm: ClickerFarm, tier: str) -> None:
    """D-03: UR сбрасывает ОБА счётчика; SSR сбрасывает только pity_ssr
    (pity_ur продолжает копиться до собственного порога); SR инкрементирует
    оба."""
    if tier == "UR":
        farm.pity_ssr = 0
        farm.pity_ur = 0
    elif tier == "SSR":
        farm.pity_ssr = 0
        farm.pity_ur += 1
    else:
        farm.pity_ssr += 1
        farm.pity_ur += 1


# --- Выбор персонажа + rate-up баннер (D-03) ---------------------------------


async def _pick_char(session: AsyncSession, chat_id: int, tier: str) -> gacha_catalog.Character:
    """Персонаж данного тира; для UR — rate-up баннер (`BotSetting
    gacha_banner`, только для UR) смещает выбор в пользу забаннеренного
    персонажа с весом `RATE_UP_WEIGHT`."""
    chars = gacha_catalog.chars_of_tier(tier)
    if tier == "UR":
        banner_id = await settings_service.get_setting(session, chat_id, GACHA_BANNER_KEY, "")
        banner_char = next((c for c in chars if c.char_id == banner_id), None)
        others = [c for c in chars if c.char_id != banner_id]
        if banner_char is not None and others:
            if _rng.random() < RATE_UP_WEIGHT:
                return banner_char
            return _rng.choice(others)
    return _rng.choice(chars)


# --- Грант + дубль -> звезда/refund (D-03) -----------------------------------


async def _select_collection_row(
    session: AsyncSession, chat_id: int, user_id: int, char_id: str
) -> GachaCollection | None:
    return (
        await session.execute(
            select(GachaCollection).where(
                GachaCollection.user_id == user_id,
                GachaCollection.chat_id == chat_id,
                GachaCollection.char_id == char_id,
            )
        )
    ).scalar_one_or_none()


async def _apply_dupe(
    session: AsyncSession,
    chat_id: int,
    user_id: int,
    char: gacha_catalog.Character,
    row: GachaCollection,
) -> dict:
    """Дубль: +1 copies, +1★ до MAX_STARS; сверх — refund ювиками (mint через
    `economy_service.credit`, D-03), звёзды выше MAX_STARS не растут. ref_id
    включает `copies` (уникален на каждый следующий дубль сверх 5★), чтобы
    несколько последовательных рефандов одного персонажа не считались одним
    и тем же replay."""
    row.copies += 1
    refunded = 0
    if row.stars < gacha_catalog.MAX_STARS:
        row.stars += 1
    else:
        refunded = gacha_catalog.DUPE_REFUND[char.tier]
        await economy_service.credit(
            session,
            chat_id,
            user_id,
            refunded,
            kind="gacha_refund",
            ref_id=f"gacha_refund:{chat_id}:{user_id}:{char.char_id}:{row.copies}",
        )
    await session.flush()
    return {"char_id": char.char_id, "tier": char.tier, "stars": row.stars, "refunded": refunded}


async def _grant(
    session: AsyncSession, chat_id: int, user_id: int, char: gacha_catalog.Character
) -> dict:
    """Грант персонажа: select-перед-insert по (user_id, chat_id, char_id);
    новый — insert(stars=1, copies=1) + `session.flush()` немедленно (чтобы
    повтор того же чара в ТОМ ЖЕ ×10 увидел уже вставленную строку, иначе
    UniqueViolation, D-03); race с конкурентной сессией — SAVEPOINT-рестарт
    (04.1-PATTERNS "race-safe partial-UNIQUE + rollback")."""
    existing = await _select_collection_row(session, chat_id, user_id, char.char_id)
    if existing is not None:
        return await _apply_dupe(session, chat_id, user_id, char, existing)

    try:
        async with session.begin_nested():
            session.add(
                GachaCollection(
                    chat_id=chat_id, user_id=user_id, char_id=char.char_id, stars=1, copies=1
                )
            )
            await session.flush()
    except IntegrityError:
        existing = await _select_collection_row(session, chat_id, user_id, char.char_id)
        if existing is None:
            raise
        return await _apply_dupe(session, chat_id, user_id, char, existing)

    return {"char_id": char.char_id, "tier": char.tier, "stars": 1, "refunded": 0}


# --- ×10 SR-гарант (D-03) -----------------------------------------------------


def _enforce_sr_guarantee(tiers: list[str]) -> list[str]:
    """Если среди 10 пиков нет SR-or-better — апгрейдит первый пик до SR.
    Под текущими весами (D-07: `TIER_WEIGHTS` без тиров ниже SR) технически
    НЕДОСТИЖИМО — любой ролл уже SR-or-better — но защита сохраняется
    буквально по плану на случай будущего изменения весов."""
    if any(t in _SR_OR_BETTER for t in tiers):
        return tiers
    tiers = list(tiers)
    tiers[0] = "SR"
    return tiers


# --- roll (D-03: 300/×10=2700, идемпотентно) ---------------------------------


async def roll(session: AsyncSession, chat_id: int, user_id: int, count: int, ref_id: str) -> dict:
    """Ролл (count=1 либо 10). Стоимость — ТОЛЬКО через `economy_service`
    (debit игрока + credit_bank = сток в банк, D-03); повтор с тем же
    `ref_id` — идемпотентный no-op (`replay=True`, деньги не двигаются
    повторно, гранты не создаются). Pity читается/пишется на строке фермы
    (общая с `clicker_service`), гранты — в `GachaCollection` (`_grant`)."""
    if count not in (1, 10):
        raise GachaError("count должен быть 1 или 10")
    cost = ROLL_COST if count == 1 else ROLL10_COST

    debited = await economy_service.debit(
        session, chat_id, user_id, cost, kind="gacha_roll", ref_id=ref_id
    )
    if not debited:
        logger.info("roll: ref_id=%s уже обработан, пропускаем", ref_id)
        await session.commit()
        return {"cost": cost, "results": [], "replay": True}

    await economy_service.credit_bank(
        session, chat_id, cost, kind="gacha_roll", ref_id=f"{ref_id}:bank"
    )

    farm = await clicker_service._get_or_create_farm(session, chat_id, user_id)

    tiers: list[str] = []
    for _ in range(count):
        tier = _pick_tier(farm)
        _apply_pity(farm, tier)
        tiers.append(tier)

    if count == 10:
        tiers = _enforce_sr_guarantee(tiers)

    results = []
    for tier in tiers:
        char = await _pick_char(session, chat_id, tier)
        results.append(await _grant(session, chat_id, user_id, char))

    await session.commit()
    return {"cost": cost, "results": results}
