"""Ферма-кликер (04.1) — тапы, оффлайн-автокликер, CP-апгрейды.

CP (`ClickerFarm.cp`) — ферма-внутренняя валюта. Этот модуль НИКОГДА не пишет
`user_balance`/`chat_bank`/`economy_tx` и не импортирует `economy_service` —
мост CP<->ювик (AMM) — отдельный план 04.1-05, который расширит этот же
модуль новыми функциями поверх той же таблицы `clicker_farms`.

Формулы — D-03 (`04-CONTEXT.md`) + REFERENCE-XYLOZ.md §3.1 (`CLICKER_*`
константы эталона xyloz_tg_bot), переносятся точно:
- Анти-чит тапов (T-04.1-12): клиентский `count` НИКОГДА не доверяем напрямую
  — `accepted = min(count, max(1, int(MAX_CPS*elapsed_ms/1000)))`.
- Оффлайн-накопление автокликера (T-04.1-13): считается НА КАЖДОМ обращении
  (`_accrue_offline`), а не фоновым тиком на юзера — `elapsed` берётся из
  разницы `now - last_accrued_at` (серверных значений), клиент не может
  подделать elapsed для начисления; капается `MAX_OFFLINE_SECONDS` (4ч).
- Стоимость апгрейда (T-04.1-14): `int(round(base * UPGRADE_GROWTH**level))`,
  считается сервером, при нехватке CP апгрейд отклоняется `ClickerError`.
"""

from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from common.models.clicker_farm import ClickerFarm

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


def _accrue_offline(farm: ClickerFarm) -> None:
    """D-03: оффлайн-накопление на КАЖДОМ обращении, без фонового тика на
    юзера. `elapsed` — разница серверных `last_accrued_at`/`utcnow()`,
    зажатая в [0, MAX_OFFLINE_SECONDS] (нижняя граница — защита от отрицательного
    elapsed при возможном рассинхроне часов; `auto_level=0` -> ничего не
    начисляется)."""
    now = datetime.utcnow()
    raw_elapsed = (now - farm.last_accrued_at).total_seconds()
    elapsed = max(0.0, min(raw_elapsed, MAX_OFFLINE_SECONDS))
    if elapsed > 0 and farm.auto_level > 0:
        farm.cp += int(farm.auto_level * AUTO_CP_PER_LEVEL_PER_SEC * elapsed)
    farm.last_accrued_at = now


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
    + коммит + текущее состояние фермы (cp/tap_level/auto_level)."""
    farm = await _get_or_create_farm(session, chat_id, user_id)
    _accrue_offline(farm)
    await session.commit()
    return _farm_state(farm)


async def tap(
    session: AsyncSession, chat_id: int, user_id: int, count: int, elapsed_ms: int
) -> dict:
    """Анти-чит тап (D-03/T-04.1-12): `accepted = min(count, max(1,
    int(MAX_CPS*elapsed_ms/1000)))` — клиентский `count` никогда не
    принимается напрямую. CP растёт на `accepted * tap_value(tap_level)`."""
    farm = await _get_or_create_farm(session, chat_id, user_id)
    _accrue_offline(farm)

    accepted = min(count, max(1, int(MAX_CPS * elapsed_ms / 1000)))
    farm.cp += accepted * tap_value(farm.tap_level)

    await session.commit()
    return _farm_state(farm, accepted=accepted)


async def _upgrade(session: AsyncSession, chat_id: int, user_id: int, base: int, level_attr: str) -> dict:
    """Общее ядро апгрейда тапа/автокликера (T-04.1-14): cost считается ДО
    списания, при нехватке CP апгрейд отклоняется без изменения состояния."""
    farm = await _get_or_create_farm(session, chat_id, user_id)
    _accrue_offline(farm)

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
