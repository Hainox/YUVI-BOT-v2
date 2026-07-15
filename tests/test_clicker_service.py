"""Интеграционные тесты clicker_service (фермы) против живого Postgres
(фикстура `session` из tests/conftest.py — транзакция-на-тест). Доказывают
farm-ядро плана 04.1-04: анти-чит тапов (accepted = min(count, max(1,
MAX_CPS×elapsed_ms/1000))), оффлайн-накопление автокликера (min(elapsed, 4ч),
без фоновых тиков на юзера), и формулу стоимости апгрейдов
(int(round(base*1.15**level))) — все три из 04-CONTEXT.md D-03.

CP — фермерская внутренняя валюта (ClickerFarm.cp); этот модуль НИКОГДА не
двигает ювики/economy_service (AMM-мост CP<->ювик — отдельный план 04.1-05).

clicker_service сам делает session.commit() там, где это описано в его
контракте — совместимо с фикстурой session благодаря join-savepoint режиму
SQLAlchemy 2.0 (тот же паттерн уже проверен в test_markets_service.py/
test_casino_service.py).
"""

from __future__ import annotations

from datetime import datetime
from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy import update

from bot.services import clicker_service
from common.models.clicker_farm import ClickerFarm
from common.models.user import User


async def _ensure_user(session, user_id: int, first_name: str = "Тест") -> None:
    session.add(User(id=user_id, first_name=first_name))
    await session.flush()


async def _get_farm(session, chat_id: int, user_id: int) -> ClickerFarm:
    return (
        await session.execute(
            select(ClickerFarm).where(
                ClickerFarm.chat_id == chat_id, ClickerFarm.user_id == user_id
            )
        )
    ).scalar_one()


async def _set_farm(session, chat_id: int, user_id: int, **values) -> None:
    """Прямая правка строки фермы в обход сервиса (детерминированная
    подготовка offline-накопления/CP без sleep()). `session.expire_all()`
    после коммита обязателен: фикстура `session` создаётся с
    `expire_on_commit=False`, поэтому уже загруженный в identity map объект
    ClickerFarm не подхватит эту прямую правку сам по себе."""
    await session.execute(
        update(ClickerFarm)
        .where(ClickerFarm.chat_id == chat_id, ClickerFarm.user_id == user_id)
        .values(**values)
    )
    await session.commit()
    session.expire_all()


# --- get_farm_state (get-or-create) ------------------------------------------


@pytest.mark.asyncio
async def test_first_access_creates_farm(session):
    chat_id = -100910001
    user_id = 910001
    await _ensure_user(session, user_id)

    state = await clicker_service.get_farm_state(session, chat_id, user_id)

    assert state["cp"] == 0
    assert state["tap_level"] == 1
    assert state["auto_level"] == 0

    farm = await _get_farm(session, chat_id, user_id)
    assert farm.cp == 0
    assert farm.tap_level == 1
    assert farm.auto_level == 0


# --- tap (D-03 анти-чит) -------------------------------------------------------


@pytest.mark.asyncio
async def test_tap_increments_cp_by_tap_value(session):
    chat_id = -100910002
    user_id = 910002
    await _ensure_user(session, user_id)
    await clicker_service.get_farm_state(session, chat_id, user_id)

    count = 5
    elapsed_ms = 1000  # max(1, int(30*1000/1000)) = 30 >= count -> не клэмпится
    result = await clicker_service.tap(session, chat_id, user_id, count, elapsed_ms)

    expected_cp = count * clicker_service.tap_value(1)
    assert result["accepted"] == count
    assert result["cp"] == expected_cp

    farm = await _get_farm(session, chat_id, user_id)
    assert farm.cp == expected_cp


@pytest.mark.asyncio
async def test_tap_anticheat_clamps_count(session):
    chat_id = -100910003
    user_id = 910003
    await _ensure_user(session, user_id)
    await clicker_service.get_farm_state(session, chat_id, user_id)

    count = 1000
    elapsed_ms = 100  # max(1, int(30*100/1000)) = max(1, 3) = 3
    result = await clicker_service.tap(session, chat_id, user_id, count, elapsed_ms)

    expected_accepted = max(1, int(clicker_service.MAX_CPS * elapsed_ms / 1000))
    assert expected_accepted < count  # sanity: клэмп реально сработал
    assert result["accepted"] == expected_accepted
    assert result["cp"] == expected_accepted * clicker_service.tap_value(1)

    farm = await _get_farm(session, chat_id, user_id)
    assert farm.cp == expected_accepted * clicker_service.tap_value(1)


@pytest.mark.asyncio
async def test_tap_anticheat_minimum_one(session):
    chat_id = -100910004
    user_id = 910004
    await _ensure_user(session, user_id)
    await clicker_service.get_farm_state(session, chat_id, user_id)

    count = 1000
    elapsed_ms = 1  # int(30*1/1000) = 0 -> max(1, 0) = 1 (пол анти-чита)
    result = await clicker_service.tap(session, chat_id, user_id, count, elapsed_ms)

    assert result["accepted"] == 1
    assert result["cp"] == clicker_service.tap_value(1)


# --- оффлайн-накопление (D-03) -------------------------------------------------


@pytest.mark.asyncio
async def test_offline_accrual_added_on_access(session):
    chat_id = -100910005
    user_id = 910005
    await _ensure_user(session, user_id)
    await clicker_service.get_farm_state(session, chat_id, user_id)

    elapsed_seconds = 100
    past = datetime.utcnow() - timedelta(seconds=elapsed_seconds)
    await _set_farm(session, chat_id, user_id, auto_level=2, last_accrued_at=past)

    state = await clicker_service.get_farm_state(session, chat_id, user_id)

    expected_gain = int(2 * clicker_service.AUTO_CP_PER_LEVEL_PER_SEC * elapsed_seconds)
    assert expected_gain > 0  # sanity: формула реально что-то начисляет
    assert state["cp"] == expected_gain

    farm = await _get_farm(session, chat_id, user_id)
    assert farm.cp == expected_gain
    assert farm.last_accrued_at > past  # накопление продвинуло метку времени вперёд


@pytest.mark.asyncio
async def test_offline_accrual_capped_4h(session):
    chat_id = -100910006
    user_id = 910006
    await _ensure_user(session, user_id)
    await clicker_service.get_farm_state(session, chat_id, user_id)

    past = datetime.utcnow() - timedelta(hours=10)
    await _set_farm(session, chat_id, user_id, auto_level=1, last_accrued_at=past)

    state = await clicker_service.get_farm_state(session, chat_id, user_id)

    expected_gain = int(
        1 * clicker_service.AUTO_CP_PER_LEVEL_PER_SEC * clicker_service.MAX_OFFLINE_SECONDS
    )
    assert state["cp"] == expected_gain


@pytest.mark.asyncio
async def test_offline_zero_when_auto_level_zero(session):
    chat_id = -100910007
    user_id = 910007
    await _ensure_user(session, user_id)
    await clicker_service.get_farm_state(session, chat_id, user_id)

    past = datetime.utcnow() - timedelta(hours=5)
    await _set_farm(session, chat_id, user_id, auto_level=0, last_accrued_at=past)

    state = await clicker_service.get_farm_state(session, chat_id, user_id)

    assert state["cp"] == 0


# --- апгрейды (D-03: int(round(base*1.15**level))) -----------------------------


@pytest.mark.asyncio
async def test_upgrade_tap_cost_formula(session):
    chat_id = -100910008
    user_id = 910008
    await _ensure_user(session, user_id)
    await clicker_service.get_farm_state(session, chat_id, user_id)

    # Достаточно CP на апгрейд — правим напрямую, без прохода через tap().
    await _set_farm(session, chat_id, user_id, cp=100_000)

    result = await clicker_service.upgrade_tap(session, chat_id, user_id)

    expected_cost = int(round(clicker_service.TAP_UPGRADE_BASE * 1.15**1))
    assert result["tap_level"] == 2
    assert result["cp"] == 100_000 - expected_cost

    farm = await _get_farm(session, chat_id, user_id)
    assert farm.tap_level == 2
    assert farm.cp == 100_000 - expected_cost


@pytest.mark.asyncio
async def test_upgrade_rejected_insufficient_cp(session):
    chat_id = -100910009
    user_id = 910009
    await _ensure_user(session, user_id)
    await clicker_service.get_farm_state(session, chat_id, user_id)

    with pytest.raises(clicker_service.ClickerError):
        await clicker_service.upgrade_tap(session, chat_id, user_id)

    farm = await _get_farm(session, chat_id, user_id)
    assert farm.cp == 0
    assert farm.tap_level == 1
