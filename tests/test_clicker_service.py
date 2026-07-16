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
from bot.services import gacha_catalog
from common.models.clicker_farm import ClickerFarm
from common.models.gacha_collection import GachaCollection
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
    # last_tap_at в прошлое — иначе серверный клэмп (CR-02) обрежет elapsed_ms
    # до ~0, т.к. farm только что создана и last_tap_at == "сейчас".
    await _set_farm(session, chat_id, user_id, last_tap_at=datetime.utcnow() - timedelta(seconds=5))

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
    await _set_farm(session, chat_id, user_id, last_tap_at=datetime.utcnow() - timedelta(seconds=5))

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
    await _set_farm(session, chat_id, user_id, last_tap_at=datetime.utcnow() - timedelta(seconds=5))

    count = 1000
    elapsed_ms = 1  # int(30*1/1000) = 0 -> max(1, 0) = 1 (пол анти-чита)
    result = await clicker_service.tap(session, chat_id, user_id, count, elapsed_ms)

    assert result["accepted"] == 1
    assert result["cp"] == clicker_service.tap_value(1)


@pytest.mark.asyncio
async def test_tap_anticheat_ignores_inflated_client_elapsed(session):
    """CR-02: клиент не может разогнать анти-чит, заявляя огромный elapsed_ms
    на каждый запрос — клэмп берёт min(client_elapsed_ms, реальный серверный
    интервал с last_tap_at). Два тапа подряд (без реальной паузы между ними)
    с фиктивным elapsed_ms=60000 должны получить `accepted` на основе
    РЕАЛЬНОГО (крошечного) интервала, а не заявленной минуты."""
    chat_id = -100910008
    user_id = 910008
    await _ensure_user(session, user_id)
    await clicker_service.get_farm_state(session, chat_id, user_id)
    await _set_farm(session, chat_id, user_id, last_tap_at=datetime.utcnow() - timedelta(seconds=5))

    fabricated_elapsed_ms = 60_000  # клиент лжёт: "прошла минута"
    first = await clicker_service.tap(session, chat_id, user_id, 5000, fabricated_elapsed_ms)
    # Первый тап всё ещё видит last_tap_at 5с в прошлом -> клэмп по 5с легитимен.
    assert first["accepted"] <= max(1, int(clicker_service.MAX_CPS * 5000))

    second = await clicker_service.tap(session, chat_id, user_id, 5000, fabricated_elapsed_ms)
    # Второй тап случился практически сразу после первого (реальный elapsed ~0),
    # поэтому даже с тем же заявленным elapsed_ms=60000 клэмп должен схлопнуться
    # к полу анти-чита (1), а не повторно принять до MAX_CPS*60 тапов.
    assert second["accepted"] == 1


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


# --- wipe_farm (FARM-03: /farmwipe backend) ------------------------------


@pytest.mark.asyncio
async def test_wipe_farm_resets_tap_auto_cp(session):
    chat_id = -100910010
    user_id = 910010
    await _ensure_user(session, user_id)
    await clicker_service.get_farm_state(session, chat_id, user_id)
    await _set_farm(session, chat_id, user_id, cp=5000, tap_level=7, auto_level=3)

    state = await clicker_service.wipe_farm(session, chat_id, user_id)

    assert state["cp"] == 0
    assert state["tap_level"] == 1
    assert state["auto_level"] == 0

    farm = await _get_farm(session, chat_id, user_id)
    assert farm.cp == 0
    assert farm.tap_level == 1
    assert farm.auto_level == 0


@pytest.mark.asyncio
async def test_wipe_farm_creates_farm_if_missing(session):
    """Ферма ещё не существует (нет ни одного обращения) — wipe_farm всё
    равно должен успешно вернуть чистое состояние (get-or-create, форма
    остальных публичных функций clicker_service)."""
    chat_id = -100910011
    user_id = 910011
    await _ensure_user(session, user_id)

    state = await clicker_service.wipe_farm(session, chat_id, user_id)

    assert state["cp"] == 0
    assert state["tap_level"] == 1
    assert state["auto_level"] == 0


# --- GACHA-02: доход фермы от собранных worker-персонажей -----------------


async def _grant_char(session, chat_id: int, user_id: int, char_id: str, stars: int = 1) -> None:
    session.add(
        GachaCollection(chat_id=chat_id, user_id=user_id, char_id=char_id, stars=stars, copies=stars)
    )
    await session.flush()


@pytest.mark.asyncio
async def test_worker_collection_increases_cp_per_sec_offline_accrual(session):
    """GACHA-02: пользователь с собранным worker-персонажем накапливает
    БОЛЬШЕ CP за оффлайн-период, чем пользователь с пустой коллекцией —
    доказывает реальную связь коллекции с доходом фермы (не заглушка)."""
    chat_id = -100910012
    with_worker_id = 910012
    empty_collection_id = 910013
    await _ensure_user(session, with_worker_id)
    await _ensure_user(session, empty_collection_id)
    await clicker_service.get_farm_state(session, chat_id, with_worker_id)
    await clicker_service.get_farm_state(session, chat_id, empty_collection_id)

    worker_char = next(
        c for c in gacha_catalog.CATALOG.values() if c.role == "worker" and c.tier == "SR"
    )
    await _grant_char(session, chat_id, with_worker_id, worker_char.char_id, stars=1)

    elapsed_seconds = 200
    past = datetime.utcnow() - timedelta(seconds=elapsed_seconds)
    await _set_farm(session, chat_id, with_worker_id, auto_level=0, last_accrued_at=past)
    await _set_farm(session, chat_id, empty_collection_id, auto_level=0, last_accrued_at=past)

    state_with_worker = await clicker_service.get_farm_state(session, chat_id, with_worker_id)
    state_empty = await clicker_service.get_farm_state(session, chat_id, empty_collection_id)

    assert state_with_worker["cp"] > state_empty["cp"]
    assert state_empty["cp"] == 0  # auto_level=0, пустая коллекция -> без дохода вовсе

    expected_rate = clicker_service.WORKER_TIER_CP_PER_SEC[worker_char.tier] * gacha_catalog.star_mult(1)
    expected_gain = int(expected_rate * elapsed_seconds)
    assert state_with_worker["cp"] == expected_gain
    assert state_with_worker["cp_per_sec"] == pytest.approx(expected_rate)


@pytest.mark.asyncio
async def test_heroine_role_does_not_contribute_farm_income(session):
    """GACHA-02: heroine-тир персонажи (role="heroine") НЕ участвуют в
    доходе фермы — только worker."""
    chat_id = -100910013
    user_id = 910014
    await _ensure_user(session, user_id)
    await clicker_service.get_farm_state(session, chat_id, user_id)

    heroine_char = next(
        c for c in gacha_catalog.CATALOG.values() if c.role == "heroine" and c.tier == "SR"
    )
    await _grant_char(session, chat_id, user_id, heroine_char.char_id, stars=1)

    elapsed_seconds = 200
    past = datetime.utcnow() - timedelta(seconds=elapsed_seconds)
    await _set_farm(session, chat_id, user_id, auto_level=0, last_accrued_at=past)

    state = await clicker_service.get_farm_state(session, chat_id, user_id)

    assert state["cp"] == 0
    assert state["cp_per_sec"] == 0
