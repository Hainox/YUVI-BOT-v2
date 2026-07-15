"""Интеграционные тесты gacha_service против живого Postgres (фикстура
`session` из tests/conftest.py — транзакция-на-тест). Доказывают гача-ядро
(04.1-06, GACHA-01..03 бэкенд): D-03 (стоимость 300/×10=2700 сток в банк,
веса SR 0.80/SSR 0.18/UR 0.02, pity SSR 50/UR 90 со сбросом обоих при UR,
rate-up баннер 0.5 только для UR, дубль +1★ до 5 затем refund ювиками
R20/SR80/SSR300/UR1500, ×10-гарант SR, идемпотентность по ref_id) и D-07
(R существует в каталоге, но НЕДОСТИЖИМ через /roll).

Все исходы форсируются через RNG-сим `gacha_service._rng` (monkeypatched
`_ForcedRng`, форма `casino_service._rng`/`_ForcedRng`) — кроме теста
распределения весов и rate-up баннера, где сознательно используется РЕАЛЬНЫЙ
`secrets.SystemRandom()`, чтобы проверить настоящую статистику (широкие,
не-flaky допуски).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import select

from bot.services import economy_service
from bot.services import gacha_catalog
from bot.services import gacha_service
from bot.services import settings_service
from common.models.chat_bank import ChatBank
from common.models.clicker_farm import ClickerFarm
from common.models.gacha_collection import GachaCollection
from common.models.user import User
from common.models.user_balance import UserBalance


async def _ensure_user(session, user_id: int, first_name: str = "Тест") -> None:
    session.add(User(id=user_id, first_name=first_name))
    await session.flush()


async def _fund(session, chat_id: int, user_id: int) -> int:
    """Заводит кошелёк (стартовый бонус economy_start_bonus) и коммитит."""
    return await economy_service.get_balance(session, chat_id, user_id)


async def _get_user_balance(session, chat_id: int, user_id: int) -> int:
    result = await session.execute(
        select(UserBalance.balance).where(
            UserBalance.chat_id == chat_id, UserBalance.user_id == user_id
        )
    )
    return result.scalar_one()


async def _get_bank_balance(session, chat_id: int) -> int:
    result = await session.execute(select(ChatBank.balance).where(ChatBank.chat_id == chat_id))
    return result.scalar_one_or_none() or 0


async def _get_farm(session, chat_id: int, user_id: int) -> ClickerFarm:
    return (
        await session.execute(
            select(ClickerFarm).where(ClickerFarm.chat_id == chat_id, ClickerFarm.user_id == user_id)
        )
    ).scalar_one()


async def _get_gacha_row(session, chat_id: int, user_id: int, char_id: str) -> GachaCollection:
    return (
        await session.execute(
            select(GachaCollection).where(
                GachaCollection.chat_id == chat_id,
                GachaCollection.user_id == user_id,
                GachaCollection.char_id == char_id,
            )
        )
    ).scalar_one()


class _ForcedRng:
    """Тестовый RNG-стаб, monkeypatched вместо `gacha_service._rng` (форма
    `casino_service._ForcedRng`). `random()` форсирует взвешенный выбор тира
    (см. `gacha_service._weighted_choice`), `choice(seq)` форсирует выбор
    персонажа по индексу."""

    def __init__(self, random_value: float = 0.0, choice_index: int = 0):
        self._random_value = random_value
        self._choice_index = choice_index

    def random(self) -> float:
        return self._random_value

    def choice(self, seq):
        return seq[self._choice_index % len(seq)]


# --- D-07: R существует в каталоге, но недостижим через ролл -----------------


def test_r_tier_exists_in_catalog_but_unreachable_via_roll():
    assert gacha_catalog.chars_of_tier("R")
    assert "R" not in gacha_catalog.TIER_WEIGHTS


# --- Стоимость ролла (D-03: 300 / ×10=2700, сток в банк) ---------------------


@pytest.mark.asyncio
async def test_roll_cost_debited_to_bank(session, monkeypatch):
    chat_id = -100910001
    user_id = 910001
    await _ensure_user(session, user_id)
    balance_before = await _fund(session, chat_id, user_id)
    bank_before = await _get_bank_balance(session, chat_id)

    monkeypatch.setattr(gacha_service, "_rng", _ForcedRng(random_value=0.0, choice_index=0))

    result = await gacha_service.roll(session, chat_id, user_id, 1, "test_roll_cost")

    assert result["cost"] == gacha_service.ROLL_COST == 300
    assert await _get_user_balance(session, chat_id, user_id) == balance_before - 300
    assert await _get_bank_balance(session, chat_id) == bank_before + 300


@pytest.mark.asyncio
async def test_roll10_costs_2700_and_returns_10(session, monkeypatch):
    chat_id = -100910002
    user_id = 910002
    await _ensure_user(session, user_id)
    balance_before = await _fund(session, chat_id, user_id)
    bank_before = await _get_bank_balance(session, chat_id)

    monkeypatch.setattr(gacha_service, "_rng", _ForcedRng(random_value=0.0, choice_index=0))

    result = await gacha_service.roll(session, chat_id, user_id, 10, "test_roll10_cost")

    assert result["cost"] == gacha_service.ROLL10_COST == 2700
    assert len(result["results"]) == 10
    assert await _get_user_balance(session, chat_id, user_id) == balance_before - 2700
    assert await _get_bank_balance(session, chat_id) == bank_before + 2700


# --- Веса тиров (D-03/D-07: SR 0.80/SSR 0.18/UR 0.02, R никогда) -------------


def test_tier_weights_distribution():
    """Свежая (не duck-typed через ORM) фарма с pity=0 — реальный `_rng`,
    широкие (~10+ std) допуски, чтобы не флакать."""
    farm = SimpleNamespace(pity_ssr=0, pity_ur=0)
    counts = {"SR": 0, "SSR": 0, "UR": 0}
    n = 400
    for _ in range(n):
        tier = gacha_service._pick_tier(farm)
        assert tier in ("SR", "SSR", "UR")  # R НЕДОСТИЖИМ (D-07)
        counts[tier] += 1

    assert 220 <= counts["SR"] <= 380  # ожидание ~320 (0.80)
    assert 15 <= counts["SSR"] <= 140  # ожидание ~72 (0.18)
    assert counts["UR"] <= 35  # ожидание ~8 (0.02), допуск только сверху


# --- Pity (D-03: SSR 50 / UR 90, UR сбрасывает оба) --------------------------


@pytest.mark.asyncio
async def test_pity_ssr_forces_ssr_at_threshold(session, monkeypatch):
    chat_id = -100910003
    user_id = 910003
    await _ensure_user(session, user_id)
    await _fund(session, chat_id, user_id)

    session.add(
        ClickerFarm(
            chat_id=chat_id,
            user_id=user_id,
            pity_ssr=gacha_catalog.PITY_SSR - 1,
            pity_ur=gacha_catalog.PITY_SSR - 1,
        )
    )
    await session.commit()

    # random_value=0.0 => при форсированном {"SSR":0.18,"UR":0.02} выбор
    # кумулятивно падает на SSR первым (детерминированно, не UR).
    monkeypatch.setattr(gacha_service, "_rng", _ForcedRng(random_value=0.0, choice_index=0))

    result = await gacha_service.roll(session, chat_id, user_id, 1, "test_pity_ssr")

    assert result["results"][0]["tier"] == "SSR"

    farm_after = await _get_farm(session, chat_id, user_id)
    assert farm_after.pity_ssr == 0  # сброс на SSR
    assert farm_after.pity_ur == gacha_catalog.PITY_SSR  # pity_ur продолжает копиться


@pytest.mark.asyncio
async def test_pity_ur_forces_ur_and_resets_both(session, monkeypatch):
    chat_id = -100910004
    user_id = 910004
    await _ensure_user(session, user_id)
    await _fund(session, chat_id, user_id)

    session.add(
        ClickerFarm(
            chat_id=chat_id,
            user_id=user_id,
            pity_ssr=10,  # намеренно ниже своего порога — доказывает, что
            pity_ur=gacha_catalog.PITY_UR - 1,  # UR-pity форсирует НЕЗАВИСИМО от pity_ssr
        )
    )
    await session.commit()

    monkeypatch.setattr(gacha_service, "_rng", _ForcedRng(random_value=0.0, choice_index=0))

    result = await gacha_service.roll(session, chat_id, user_id, 1, "test_pity_ur")

    assert result["results"][0]["tier"] == "UR"

    farm_after = await _get_farm(session, chat_id, user_id)
    assert farm_after.pity_ssr == 0  # UR сбрасывает ОБА, даже не-пороговый pity_ssr
    assert farm_after.pity_ur == 0


# --- Rate-up баннер (D-03: только UR, вес 0.5) -------------------------------


@pytest.mark.asyncio
async def test_rate_up_banner_biases_ur(session):
    chat_id = -100910005
    banner_char = gacha_catalog.chars_of_tier("UR")[0]

    settings_service.clear_cache()
    await settings_service.set_setting(
        session, chat_id, gacha_service.GACHA_BANNER_KEY, banner_char.char_id, updated_by_tg_id=1
    )
    await session.commit()

    n = 300
    banner_count = 0
    for _ in range(n):
        char = await gacha_service._pick_char(session, chat_id, "UR")
        if char.char_id == banner_char.char_id:
            banner_count += 1

    # Наивная равномерность среди 3 UR-персонажей каталога дала бы ~100 из
    # 300 (1/3) — rate-up (вес 0.5) должен дать заметно больше, широкий
    # допуск против флакающего теста.
    assert banner_count > 110
    assert banner_count < 200

    settings_service.clear_cache()


# --- Дубль -> звезда/refund (D-03: +1★ до 5, сверх — refund ювиками) ---------


@pytest.mark.asyncio
async def test_dupe_adds_star_up_to_5(session, monkeypatch):
    chat_id = -100910006
    user_id = 910006
    await _ensure_user(session, user_id)
    await _fund(session, chat_id, user_id)

    monkeypatch.setattr(gacha_service, "_rng", _ForcedRng(random_value=0.0, choice_index=0))
    char = gacha_catalog.chars_of_tier("SR")[0]

    for i in range(1, 6):
        result = await gacha_service.roll(session, chat_id, user_id, 1, f"test_dupe_star_{i}")
        grant = result["results"][0]
        assert grant["char_id"] == char.char_id
        assert grant["stars"] == i
        assert grant["refunded"] == 0

    row = await _get_gacha_row(session, chat_id, user_id, char.char_id)
    assert row.stars == 5
    assert row.copies == 5


@pytest.mark.asyncio
async def test_dupe_over_5_refunds(session, monkeypatch):
    chat_id = -100910007
    user_id = 910007
    await _ensure_user(session, user_id)
    await _fund(session, chat_id, user_id)

    monkeypatch.setattr(gacha_service, "_rng", _ForcedRng(random_value=0.0, choice_index=0))
    char = gacha_catalog.chars_of_tier("SR")[0]

    for i in range(1, 6):
        await gacha_service.roll(session, chat_id, user_id, 1, f"test_dupe_refund_build_{i}")

    balance_before_6th = await _get_user_balance(session, chat_id, user_id)

    result = await gacha_service.roll(session, chat_id, user_id, 1, "test_dupe_refund_6th")
    grant = result["results"][0]

    assert grant["char_id"] == char.char_id
    assert grant["stars"] == gacha_catalog.MAX_STARS
    assert grant["refunded"] == gacha_catalog.DUPE_REFUND["SR"]

    balance_after_6th = await _get_user_balance(session, chat_id, user_id)
    expected_delta = -gacha_service.ROLL_COST + gacha_catalog.DUPE_REFUND["SR"]
    assert balance_after_6th - balance_before_6th == expected_delta

    row = await _get_gacha_row(session, chat_id, user_id, char.char_id)
    assert row.stars == gacha_catalog.MAX_STARS
    assert row.copies == 6


# --- ×10 SR-гарант (D-03) -----------------------------------------------------


def test_enforce_sr_guarantee_upgrades_first_pick_when_missing():
    """Белый ящик: _enforce_sr_guarantee — под текущими весами (D-07) этот
    сценарий структурно недостижим через настоящий _pick_tier (R не может
    туда попасть), но сама защитная функция должна работать корректно."""
    tiers = ["R"] * 10
    result = gacha_service._enforce_sr_guarantee(tiers)
    assert result[0] == "SR"
    assert result[1:] == ["R"] * 9


@pytest.mark.asyncio
async def test_roll10_guarantees_sr(session, monkeypatch):
    chat_id = -100910008
    user_id = 910008
    await _ensure_user(session, user_id)
    await _fund(session, chat_id, user_id)

    # "Худшая удача" под D-07 — всё равно UR (лучше SR) на каждом пике,
    # гарантия тривиально выполняется, т.к. ниже SR тиров в весах нет.
    monkeypatch.setattr(gacha_service, "_rng", _ForcedRng(random_value=0.999, choice_index=0))

    result = await gacha_service.roll(session, chat_id, user_id, 10, "test_roll10_guarantee")

    tiers = [r["tier"] for r in result["results"]]
    assert any(t in gacha_service._SR_OR_BETTER for t in tiers)


# --- Идемпотентность replay (D-03/T-04.1-20) ---------------------------------


@pytest.mark.asyncio
async def test_roll_idempotent_on_ref_id(session, monkeypatch):
    chat_id = -100910009
    user_id = 910009
    await _ensure_user(session, user_id)
    await _fund(session, chat_id, user_id)

    monkeypatch.setattr(gacha_service, "_rng", _ForcedRng(random_value=0.0, choice_index=0))

    ref_id = "test_roll_idempotent"
    await gacha_service.roll(session, chat_id, user_id, 1, ref_id)
    balance_after_first = await _get_user_balance(session, chat_id, user_id)

    second = await gacha_service.roll(session, chat_id, user_id, 1, ref_id)

    assert second["replay"] is True
    assert second["results"] == []
    assert await _get_user_balance(session, chat_id, user_id) == balance_after_first
