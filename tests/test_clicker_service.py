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

from datetime import date
from datetime import datetime
from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy import update

from bot.services import clicker_service
from bot.services import gacha_catalog
from common.models.achievement_unlock import AchievementUnlock
from common.models.clicker_farm import ClickerFarm
from common.models.gacha_collection import GachaCollection
from common.models.quest_completion import QuestCompletion
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
async def test_upgrade_rejected_at_max_level(session, monkeypatch):
    monkeypatch.setattr(clicker_service.settings, "farm_max_level", 50)
    chat_id = -100910010
    user_id = 910010
    await _ensure_user(session, user_id)
    await clicker_service.get_farm_state(session, chat_id, user_id)
    await _set_farm(session, chat_id, user_id, cp=1_000_000_000, tap_level=50)

    with pytest.raises(clicker_service.ClickerError):
        await clicker_service.upgrade_tap(session, chat_id, user_id)

    farm = await _get_farm(session, chat_id, user_id)
    assert farm.tap_level == 50
    assert farm.cp == 1_000_000_000


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


# --- GACHA-02: доход фермы от собранных героинь -----------------------------


async def _grant_char(session, chat_id: int, user_id: int, char_id: str, stars: int = 1) -> None:
    session.add(
        GachaCollection(chat_id=chat_id, user_id=user_id, char_id=char_id, stars=stars, copies=stars)
    )
    await session.flush()


@pytest.mark.asyncio
async def test_collection_increases_cp_per_sec_offline_accrual(session):
    """GACHA-02: пользователь с собранной героиней накапливает БОЛЬШЕ CP за
    оффлайн-период, чем пользователь с пустой коллекцией — доказывает
    реальную связь коллекции с доходом фермы (не заглушка)."""
    chat_id = -100910012
    with_char_id = 910012
    empty_collection_id = 910013
    await _ensure_user(session, with_char_id)
    await _ensure_user(session, empty_collection_id)
    await clicker_service.get_farm_state(session, chat_id, with_char_id)
    await clicker_service.get_farm_state(session, chat_id, empty_collection_id)

    char = next(c for c in gacha_catalog.CATALOG.values() if c.tier == "S")
    await _grant_char(session, chat_id, with_char_id, char.char_id, stars=1)

    elapsed_seconds = 200
    past = datetime.utcnow() - timedelta(seconds=elapsed_seconds)
    await _set_farm(session, chat_id, with_char_id, auto_level=0, last_accrued_at=past)
    await _set_farm(session, chat_id, empty_collection_id, auto_level=0, last_accrued_at=past)

    state_with_char = await clicker_service.get_farm_state(session, chat_id, with_char_id)
    state_empty = await clicker_service.get_farm_state(session, chat_id, empty_collection_id)

    assert state_with_char["cp"] > state_empty["cp"]
    assert state_empty["cp"] == 0  # auto_level=0, пустая коллекция -> без дохода вовсе

    expected_rate = clicker_service.WORKER_TIER_CP_PER_SEC[char.tier] * gacha_catalog.star_mult(1)
    expected_gain = int(expected_rate * elapsed_seconds)
    assert state_with_char["cp"] == expected_gain
    assert state_with_char["cp_per_sec"] == pytest.approx(expected_rate)


@pytest.mark.asyncio
async def test_any_tier_contributes_farm_income(session):
    """GACHA-02: после снятия фильтра role доход фермы даёт ЛЮБАЯ собранная
    героиня, а не только часть тира — проверяем это на тире, отличном от
    основного теста выше (UR), чтобы доказать отсутствие скрытого фильтра."""
    chat_id = -100910013
    user_id = 910014
    await _ensure_user(session, user_id)
    await clicker_service.get_farm_state(session, chat_id, user_id)

    char = next(c for c in gacha_catalog.CATALOG.values() if c.tier == "UR")
    await _grant_char(session, chat_id, user_id, char.char_id, stars=1)

    elapsed_seconds = 200
    past = datetime.utcnow() - timedelta(seconds=elapsed_seconds)
    await _set_farm(session, chat_id, user_id, auto_level=0, last_accrued_at=past)

    state = await clicker_service.get_farm_state(session, chat_id, user_id)

    expected_rate = clicker_service.WORKER_TIER_CP_PER_SEC[char.tier] * gacha_catalog.star_mult(1)
    expected_gain = int(expected_rate * elapsed_seconds)
    assert state["cp"] == expected_gain
    assert state["cp_per_sec"] == pytest.approx(expected_rate)


# --- QUEST-01/02: эффективный потолок фермы (get_effective_max_level) --------


@pytest.mark.asyncio
async def test_effective_max_level_equals_base_with_no_unlocks(session, monkeypatch):
    """Ноль строк в quest_completions и ноль в achievement_unlocks -> эффективный
    потолок в точности равен settings.farm_max_level (без бонусов)."""
    monkeypatch.setattr(clicker_service.settings, "farm_max_level", 50)
    chat_id = -100910014
    user_id = 910015
    await _ensure_user(session, user_id)

    effective = await clicker_service.get_effective_max_level(session, chat_id, user_id)

    assert effective == 50


@pytest.mark.asyncio
async def test_effective_max_level_quest_completions_raise_it(session, monkeypatch):
    """N строк в quest_completions (любые quest_key/day_msk) поднимают потолок
    ровно на N // QUEST_COMPLETIONS_PER_BONUS_LEVEL сверх базы — floor-деление,
    не пропорция 1:1."""
    monkeypatch.setattr(clicker_service.settings, "farm_max_level", 50)
    chat_id = -100910015
    user_id = 910016
    await _ensure_user(session, user_id)

    n = 7  # не кратно QUEST_COMPLETIONS_PER_BONUS_LEVEL(3) -> проверяем floor-деление
    today = date.today()
    session.add_all(
        [
            QuestCompletion(
                chat_id=chat_id,
                user_id=user_id,
                quest_key=f"quest_{i}",
                day_msk=today,
                reward_amount=10,
            )
            for i in range(n)
        ]
    )
    await session.flush()

    effective = await clicker_service.get_effective_max_level(session, chat_id, user_id)

    assert effective == 50 + n // clicker_service.QUEST_COMPLETIONS_PER_BONUS_LEVEL


@pytest.mark.asyncio
async def test_effective_max_level_achievement_bonus_adds_sum(session, monkeypatch):
    """Строки achievement_unlocks добавляют к потолку СУММУ своих bonus_levels
    напрямую (без деления, в отличие от квестов)."""
    monkeypatch.setattr(clicker_service.settings, "farm_max_level", 50)
    chat_id = -100910016
    user_id = 910017
    await _ensure_user(session, user_id)

    session.add(
        AchievementUnlock(
            chat_id=chat_id,
            user_id=user_id,
            achievement_key="ach_a",
            bonus_levels=3,
            reward_amount=100,
        )
    )
    session.add(
        AchievementUnlock(
            chat_id=chat_id,
            user_id=user_id,
            achievement_key="ach_b",
            bonus_levels=5,
            reward_amount=100,
        )
    )
    await session.flush()

    effective = await clicker_service.get_effective_max_level(session, chat_id, user_id)

    assert effective == 50 + 3 + 5


@pytest.mark.asyncio
async def test_effective_max_level_clamped_at_ceiling(session, monkeypatch):
    """Даже если база + бонус квестов + бонус ачивок математически намного
    превышают FARM_EFFECTIVE_CAP_CEILING(99), get_effective_max_level никогда
    не возвращает больше потолка — min()-клэмп реально работает, а не просто
    присутствует в формуле."""
    monkeypatch.setattr(clicker_service.settings, "farm_max_level", 50)
    chat_id = -100910017
    user_id = 910018
    await _ensure_user(session, user_id)

    today = date.today()
    session.add_all(
        [
            QuestCompletion(
                chat_id=chat_id,
                user_id=user_id,
                quest_key=f"quest_{i}",
                day_msk=today,
                reward_amount=10,
            )
            for i in range(1000)
        ]
    )
    session.add_all(
        [
            AchievementUnlock(
                chat_id=chat_id,
                user_id=user_id,
                achievement_key=f"ach_{i}",
                bonus_levels=50,
                reward_amount=100,
            )
            for i in range(5)
        ]
    )
    await session.flush()

    # sanity: без клэмпа сырая формула реально превысила бы потолок
    quest_bonus = 1000 // clicker_service.QUEST_COMPLETIONS_PER_BONUS_LEVEL
    achievement_bonus = 5 * 50
    assert 50 + quest_bonus + achievement_bonus > clicker_service.FARM_EFFECTIVE_CAP_CEILING

    effective = await clicker_service.get_effective_max_level(session, chat_id, user_id)

    assert effective == clicker_service.FARM_EFFECTIVE_CAP_CEILING
    assert effective == 99


@pytest.mark.asyncio
async def test_upgrade_succeeds_when_quest_completions_raise_effective_cap(session, monkeypatch):
    """Сквозная интеграция: test_upgrade_rejected_at_max_level доказывает, что
    tap_level=50 при farm_max_level=50 и нулевых разблокировках блокирует
    апгрейд. Здесь та же стартовая позиция, но с quest_completions,
    поднимающими эффективный потолок до 55 (15 // 3 = 5 бонусных уровня) —
    upgrade_tap теперь должен ПРОЙТИ и поднять tap_level до 51, доказывая, что
    разблокировка реально поднимает потолок, применяемый внутри _upgrade, а
    не только результат изолированной формулы get_effective_max_level."""
    monkeypatch.setattr(clicker_service.settings, "farm_max_level", 50)
    chat_id = -100910018
    user_id = 910019
    await _ensure_user(session, user_id)
    await clicker_service.get_farm_state(session, chat_id, user_id)
    await _set_farm(session, chat_id, user_id, cp=1_000_000_000, tap_level=50)

    n = 15  # 15 // QUEST_COMPLETIONS_PER_BONUS_LEVEL(3) = 5 -> эффективный потолок 50+5=55
    today = date.today()
    session.add_all(
        [
            QuestCompletion(
                chat_id=chat_id,
                user_id=user_id,
                quest_key=f"quest_{i}",
                day_msk=today,
                reward_amount=10,
            )
            for i in range(n)
        ]
    )
    await session.flush()

    effective = await clicker_service.get_effective_max_level(session, chat_id, user_id)
    assert effective == 55  # sanity: потолок реально поднят перед апгрейдом

    result = await clicker_service.upgrade_tap(session, chat_id, user_id)

    assert result["tap_level"] == 51

    farm = await _get_farm(session, chat_id, user_id)
    assert farm.tap_level == 51
