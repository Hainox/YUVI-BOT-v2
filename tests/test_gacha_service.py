"""Интеграционные тесты gacha_service против живого Postgres (фикстура
`session` из tests/conftest.py — транзакция-на-тест). Доказывают гача-ядро
(04.1-06, GACHA-01..03 бэкенд): D-03 (стоимость 300/×10=2700 сток в банк,
веса S 0.78/UR 0.20/UUR 0.02, pity UR 50/UUR 90 со сбросом обоих при UUR,
rate-up баннер 0.5 только для UUR, дубль +1★ до 5 затем refund ювиками
R20/S80/UR400/UUR1500, ×10-гарант S, идемпотентность по ref_id) и D-07
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

from bot.services import constellation_catalog
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


async def _top_up(session, chat_id: int, user_id: int, amount: int, ref_id: str) -> int:
    """Стартового бонуса (economy_start_bonus=1000) не хватает на ×10-ролл
    (2700) или на несколько последовательных роллов подряд — довносит
    ювики через economy_service.credit (не пишет баланс напрямую)."""
    await economy_service.credit(session, chat_id, user_id, amount, kind="test_top_up", ref_id=ref_id)
    await session.commit()
    return await _get_user_balance(session, chat_id, user_id)


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
    персонажа: фиксированным индексом (по умолчанию) либо по кругу
    (`cycle=True` — каждый следующий вызов сдвигается на 1, чтобы ×10-ролл
    разошёлся по разным персонажам вместо повторного дублирования одного)."""

    def __init__(self, random_value: float = 0.0, choice_index: int = 0, cycle: bool = False):
        self._random_value = random_value
        self._choice_index = choice_index
        self._cycle = cycle
        self._call_count = 0

    def random(self) -> float:
        return self._random_value

    def choice(self, seq):
        index = (self._choice_index + self._call_count) if self._cycle else self._choice_index
        self._call_count += 1
        return seq[index % len(seq)]


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
    await _fund(session, chat_id, user_id)
    balance_before = await _top_up(session, chat_id, user_id, 10_000, "test_roll10_cost_top_up")
    bank_before = await _get_bank_balance(session, chat_id)

    # cycle=True разводит 10 пиков по разным SR-персонажам каталога — иначе
    # один и тот же чар набрал бы дубли и рефанды сверх 5★, и итоговый баланс
    # не был бы равен ровно balance_before-2700 (эта проверка — про стоимость
    # ролла, не про дубли/рефанды, см. отдельные test_dupe_*).
    monkeypatch.setattr(gacha_service, "_rng", _ForcedRng(random_value=0.0, cycle=True))

    result = await gacha_service.roll(session, chat_id, user_id, 10, "test_roll10_cost")

    assert result["cost"] == gacha_service.ROLL10_COST == 2700
    assert len(result["results"]) == 10
    assert await _get_user_balance(session, chat_id, user_id) == balance_before - 2700
    assert await _get_bank_balance(session, chat_id) == bank_before + 2700


# --- Веса тиров (D-03/D-07: S 0.78/UR 0.20/UUR 0.02, R никогда) -------------


def test_tier_weights_distribution():
    """Свежая (не duck-typed через ORM) фарма с pity=0 — реальный `_rng`,
    широкие (~10+ std) допуски, чтобы не флакать."""
    farm = SimpleNamespace(pity_ssr=0, pity_ur=0)
    counts = {"S": 0, "UR": 0, "UUR": 0}
    n = 400
    for _ in range(n):
        tier = gacha_service._pick_tier(farm)
        assert tier in ("S", "UR", "UUR")  # R НЕДОСТИЖИМ (D-07)
        counts[tier] += 1

    assert 230 <= counts["S"] <= 390  # ожидание ~312 (0.78)
    assert 20 <= counts["UR"] <= 160  # ожидание ~80 (0.20)
    assert counts["UUR"] <= 35  # ожидание ~8 (0.02), допуск только сверху


# --- Pity (D-03: UR 50 / UUR 90, UUR сбрасывает оба) --------------------------


@pytest.mark.asyncio
async def test_pity_ur_forces_ur_at_threshold(session, monkeypatch):
    chat_id = -100910003
    user_id = 910003
    await _ensure_user(session, user_id)
    await _fund(session, chat_id, user_id)

    session.add(
        ClickerFarm(
            chat_id=chat_id,
            user_id=user_id,
            pity_ssr=gacha_catalog.PITY_UR - 1,
            pity_ur=gacha_catalog.PITY_UR - 1,
        )
    )
    await session.commit()

    # random_value=0.0 => при форсированном {"UR":0.20,"UUR":0.02} выбор
    # кумулятивно падает на UR первым (детерминированно, не UUR).
    monkeypatch.setattr(gacha_service, "_rng", _ForcedRng(random_value=0.0, choice_index=0))

    result = await gacha_service.roll(session, chat_id, user_id, 1, "test_pity_ur")

    assert result["results"][0]["tier"] == "UR"

    farm_after = await _get_farm(session, chat_id, user_id)
    assert farm_after.pity_ssr == 0  # сброс на UR
    assert farm_after.pity_ur == gacha_catalog.PITY_UR  # pity_ur продолжает копиться


@pytest.mark.asyncio
async def test_pity_uur_forces_uur_and_resets_both(session, monkeypatch):
    chat_id = -100910004
    user_id = 910004
    await _ensure_user(session, user_id)
    await _fund(session, chat_id, user_id)

    session.add(
        ClickerFarm(
            chat_id=chat_id,
            user_id=user_id,
            pity_ssr=10,  # намеренно ниже своего порога — доказывает, что
            pity_ur=gacha_catalog.PITY_UUR - 1,  # UUR-pity форсирует НЕЗАВИСИМО от pity_ssr
        )
    )
    await session.commit()

    monkeypatch.setattr(gacha_service, "_rng", _ForcedRng(random_value=0.0, choice_index=0))

    result = await gacha_service.roll(session, chat_id, user_id, 1, "test_pity_uur")

    assert result["results"][0]["tier"] == "UUR"

    farm_after = await _get_farm(session, chat_id, user_id)
    assert farm_after.pity_ssr == 0  # UUR сбрасывает ОБА, даже не-пороговый pity_ssr
    assert farm_after.pity_ur == 0


# --- Rate-up баннер (D-03: только UUR, вес 0.5) -------------------------------


@pytest.mark.asyncio
async def test_rate_up_banner_biases_uur(session):
    chat_id = -100910005
    banner_char = gacha_catalog.chars_of_tier("UUR")[0]

    settings_service.clear_cache()
    await settings_service.set_setting(
        session, chat_id, gacha_service.GACHA_BANNER_KEY, banner_char.char_id, updated_by_tg_id=1
    )
    await session.commit()

    n = 300
    banner_count = 0
    for _ in range(n):
        char = await gacha_service._pick_char(session, chat_id, "UUR")
        if char.char_id == banner_char.char_id:
            banner_count += 1

    # Наивная равномерность среди 3 UUR-персонажей каталога дала бы ~100 из
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
    await _top_up(session, chat_id, user_id, 10_000, "test_dupe_star_top_up")

    monkeypatch.setattr(gacha_service, "_rng", _ForcedRng(random_value=0.0, choice_index=0))
    char = gacha_catalog.chars_of_tier("S")[0]

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
    await _top_up(session, chat_id, user_id, 10_000, "test_dupe_refund_top_up")

    monkeypatch.setattr(gacha_service, "_rng", _ForcedRng(random_value=0.0, choice_index=0))
    char = gacha_catalog.chars_of_tier("S")[0]

    for i in range(1, 6):
        await gacha_service.roll(session, chat_id, user_id, 1, f"test_dupe_refund_build_{i}")

    balance_before_6th = await _get_user_balance(session, chat_id, user_id)

    result = await gacha_service.roll(session, chat_id, user_id, 1, "test_dupe_refund_6th")
    grant = result["results"][0]

    assert grant["char_id"] == char.char_id
    assert grant["stars"] == gacha_catalog.MAX_STARS
    assert grant["refunded"] == gacha_catalog.DUPE_REFUND["S"]

    balance_after_6th = await _get_user_balance(session, chat_id, user_id)
    expected_delta = -gacha_service.ROLL_COST + gacha_catalog.DUPE_REFUND["S"]
    assert balance_after_6th - balance_before_6th == expected_delta

    row = await _get_gacha_row(session, chat_id, user_id, char.char_id)
    assert row.stars == gacha_catalog.MAX_STARS
    assert row.copies == 6


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tier,chat_id,user_id",
    [
        ("UR", -100910101, 910101),
        ("UUR", -100910102, 910102),
    ],
)
async def test_dupe_over_5_refunds_rare_tiers(session, tier, chat_id, user_id):
    """Аудит-регрессия: test_dupe_over_5_refunds раньше форсировал рефанд
    только для самого частого/дешёвого тира S (80) — редкий/дорогой UUR
    (1500, в 18.75 раза больше) и UR (400) не проверялись ни разу. Белым
    ящиком, минуя необходимость форсировать редкий тир через полный roll()
    (UUR ~0.02 базового веса) — сразу вызываем _grant/_apply_dupe на уже
    подготовленной строке GachaCollection(stars=5, copies=5), как предложено
    в аудите, и проверяем как поле `refunded` в ответе, так и реальную
    дельту баланса."""
    await _ensure_user(session, user_id)
    await _fund(session, chat_id, user_id)

    char = gacha_catalog.chars_of_tier(tier)[0]
    session.add(
        GachaCollection(
            chat_id=chat_id,
            user_id=user_id,
            char_id=char.char_id,
            stars=gacha_catalog.MAX_STARS,
            copies=gacha_catalog.MAX_STARS,
        )
    )
    await session.commit()

    balance_before = await _get_user_balance(session, chat_id, user_id)

    grant = await gacha_service._grant(session, chat_id, user_id, char)
    await session.commit()

    assert grant["char_id"] == char.char_id
    assert grant["stars"] == gacha_catalog.MAX_STARS  # звёзды не растут выше MAX_STARS
    assert grant["refunded"] == gacha_catalog.DUPE_REFUND[tier]

    balance_after = await _get_user_balance(session, chat_id, user_id)
    assert balance_after - balance_before == gacha_catalog.DUPE_REFUND[tier]

    row = await _get_gacha_row(session, chat_id, user_id, char.char_id)
    assert row.stars == gacha_catalog.MAX_STARS
    assert row.copies == gacha_catalog.MAX_STARS + 1


@pytest.mark.asyncio
async def test_dupe_refund_not_reported_when_credit_replays(session, monkeypatch):
    """Аудит-регрессия: `economy_service.credit` — идемпотентная операция,
    возвращающая False, если ref_id рефанда уже применялся (деньги НЕ
    начисляются повторно). `_apply_dupe` раньше клал `refunded` в ответ
    безусловно, не проверяя возврат credit — Mini App показал бы игроку
    ложное "вам начислено N", хотя баланс не изменился. Монки-патчим
    economy_service.credit так, чтобы вернуть False именно на ноге рефанда
    гачи (kind="gacha_refund"), остальные вызовы (debit/credit_bank/старт-
    бонус/top_up) проксируются к реальной реализации."""
    chat_id = -100910104
    user_id = 910104
    await _ensure_user(session, user_id)
    await _fund(session, chat_id, user_id)
    await _top_up(session, chat_id, user_id, 10_000, "test_refund_replay_top_up")

    monkeypatch.setattr(gacha_service, "_rng", _ForcedRng(random_value=0.0, choice_index=0))
    char = gacha_catalog.chars_of_tier("S")[0]

    for i in range(1, 6):
        await gacha_service.roll(session, chat_id, user_id, 1, f"test_refund_replay_build_{i}")

    real_credit = economy_service.credit

    async def _fake_credit(session_, chat_id_, user_id_, amount, kind, ref_id):
        if kind == "gacha_refund":
            return False  # симулируем коллизию ref_id рефанда (идемпотентный replay)
        return await real_credit(session_, chat_id_, user_id_, amount, kind=kind, ref_id=ref_id)

    monkeypatch.setattr(economy_service, "credit", _fake_credit)

    balance_before_6th = await _get_user_balance(session, chat_id, user_id)

    result = await gacha_service.roll(session, chat_id, user_id, 1, "test_refund_replay_6th")
    grant = result["results"][0]

    assert grant["char_id"] == char.char_id
    assert grant["stars"] == gacha_catalog.MAX_STARS
    assert grant["refunded"] == 0  # credit вернул False -> рефанд НЕ считается применённым

    balance_after_6th = await _get_user_balance(session, chat_id, user_id)
    # Списана только стоимость ролла — рефанд НЕ начислен (в отличие от
    # обычного случая, см. test_dupe_over_5_refunds_rare_tiers/_adds_star).
    assert balance_after_6th - balance_before_6th == -gacha_service.ROLL_COST

    row = await _get_gacha_row(session, chat_id, user_id, char.char_id)
    assert row.stars == gacha_catalog.MAX_STARS
    assert row.copies == 6


@pytest.mark.asyncio
async def test_grant_race_integrity_error_falls_back_to_dupe(session, monkeypatch):
    """Аудит-регрессия: SAVEPOINT-рестарт в _grant на конкурентную гонку
    (докстринг _grant: "race-safe SAVEPOINT-рестарт", форма
    markets_service.import_market) не был покрыт ни одним тестом. Форсируем
    РЕАЛЬНУЮ гонку: строка GachaCollection для этого char_id уже существует
    и закоммичена (имитация "выигравшей" конкурентной транзакции), но
    первый select-check внутри _grant монки-патчится, чтобы вернуть None
    (имитация узкого окна гонки — SELECT не увидел ещё не видимую на тот
    момент строку) — INSERT внутри session.begin_nested() тогда реально
    конфликтует с UNIQUE(user_id, chat_id, char_id) и поднимает настоящий
    IntegrityError; код обязан перехватить его, перечитать строку (второй
    вызов _select_collection_row — уже НЕ монки-патченный) и корректно
    переключиться на _apply_dupe, а не упасть необработанным исключением
    или создать вторую строку."""
    chat_id = -100910105
    user_id = 910105
    await _ensure_user(session, user_id)
    await _fund(session, chat_id, user_id)

    char = gacha_catalog.chars_of_tier("S")[0]
    session.add(
        GachaCollection(chat_id=chat_id, user_id=user_id, char_id=char.char_id, stars=1, copies=1)
    )
    await session.commit()

    real_select = gacha_service._select_collection_row
    call_count = 0

    async def _select_first_none(session_, chat_id_, user_id_, char_id_):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return None  # окно гонки: конкурентная строка ещё "не видна"
        return await real_select(session_, chat_id_, user_id_, char_id_)

    monkeypatch.setattr(gacha_service, "_select_collection_row", _select_first_none)

    grant = await gacha_service._grant(session, chat_id, user_id, char)
    await session.commit()

    assert grant["char_id"] == char.char_id
    assert grant["stars"] == 2  # дубль применился к существующей строке
    assert grant["refunded"] == 0

    # scalar_one() внутри _get_gacha_row сам по себе доказывает отсутствие
    # второй (дублирующей) строки — упал бы с MultipleResultsFound.
    row = await _get_gacha_row(session, chat_id, user_id, char.char_id)
    assert row.stars == 2
    assert row.copies == 2


# --- ×10 S-гарант (D-03) ------------------------------------------------------


def test_enforce_s_guarantee_upgrades_first_pick_when_missing():
    """Белый ящик: _enforce_s_guarantee — под текущими весами (D-07) этот
    сценарий структурно недостижим через настоящий _pick_tier (R не может
    туда попасть), но сама защитная функция должна работать корректно."""
    tiers = ["R"] * 10
    result = gacha_service._enforce_s_guarantee(tiers)
    assert result[0] == "S"
    assert result[1:] == ["R"] * 9


@pytest.mark.asyncio
async def test_roll10_guarantees_s(session, monkeypatch):
    chat_id = -100910008
    user_id = 910008
    await _ensure_user(session, user_id)
    await _fund(session, chat_id, user_id)
    await _top_up(session, chat_id, user_id, 10_000, "test_roll10_guarantee_top_up")

    # "Худшая удача" под D-07 — всё равно UR/UUR (лучше S) на каждом пике,
    # гарантия тривиально выполняется, т.к. ниже S тиров в весах нет.
    monkeypatch.setattr(gacha_service, "_rng", _ForcedRng(random_value=0.999, choice_index=0))

    result = await gacha_service.roll(session, chat_id, user_id, 10, "test_roll10_guarantee")

    tiers = [r["tier"] for r in result["results"]]
    assert any(t in gacha_service._S_OR_BETTER for t in tiers)


# --- Откат на исключение между debit/credit_bank и финальным commit ---------


@pytest.mark.asyncio
async def test_roll_exception_mid_grant_does_not_fabricate_or_double_charge(session, monkeypatch):
    """Аудит-регрессия: roll() списывает стоимость (debit+credit_bank), затем
    крутит гранты — всё в ОДНОЙ незакоммиченной сессии, коммитя один раз в
    самом конце (строка 259). Форсируем RuntimeError на 2-м гранте ×10-ролла
    (уже ПОСЛЕ debit/credit_bank).

    Прим. про рамки теста (форма
    tests/test_casino_service.py::test_settle_payout_failure_after_rng_does_not_fabricate_or_double_charge
    и tests/test_duel_service.py::test_accept_duel_payout_failure_after_rng_does_not_fabricate_or_double_charge):
    фикстура `session` (tests/conftest.py) кладёт AsyncSession поверх уже
    открытой внешней транзакции соединения — явный `session.rollback()`
    здесь откатил бы ВСЮ транзакцию теста целиком (включая уже
    закоммиченный сетап _ensure_user/_fund/_top_up), экспериментально
    проверено (упомянутая гонка воспроизводится: после rollback() строка
    UserBalance пропадает целиком). Поэтому здесь проверяем денежный
    инвариант БЕЗ явного rollback: debit/credit_bank реально произошли (деньги
    "в подвешенном" состоянии внутри текущей, ещё не завершённой транзакции,
    ждут итога всего roll()) — а наивный повтор с ТЕМ ЖЕ ref_id (то, что
    реально сделал бы клиент после сетевого таймаута) обязан вернуть
    идемпотентный no-op (replay=True), а не списать стоимость повторно."""
    chat_id = -100910103
    user_id = 910103
    await _ensure_user(session, user_id)
    await _fund(session, chat_id, user_id)
    balance_before = await _top_up(session, chat_id, user_id, 10_000, "test_rollback_mid_grant_top_up")
    bank_before = await _get_bank_balance(session, chat_id)

    # cycle=True — некритично здесь (падаем до дублей), но следует тому же
    # паттерну, что test_roll10_costs_2700_and_returns_10.
    monkeypatch.setattr(gacha_service, "_rng", _ForcedRng(random_value=0.0, cycle=True))

    real_grant = gacha_service._grant
    call_count = 0

    async def _grant_boom_on_second(session_, chat_id_, user_id_, char):
        nonlocal call_count
        call_count += 1
        if call_count == 2:
            raise RuntimeError("boom: симулированный сбой после debit/credit_bank, до commit")
        return await real_grant(session_, chat_id_, user_id_, char)

    monkeypatch.setattr(gacha_service, "_grant", _grant_boom_on_second)

    ref_id = "test_rollback_mid_grant"
    with pytest.raises(RuntimeError):
        await gacha_service.roll(session, chat_id, user_id, 10, ref_id)

    assert call_count == 2  # доказывает, что debit/credit_bank уже прошли (farm/tiers посчитаны)

    # Списание уже реально произошло ДО сбоя гранта — деньги "в подвешенном"
    # состоянии внутри текущей (пока не завершённой) транзакции.
    assert await _get_user_balance(session, chat_id, user_id) == balance_before - gacha_service.ROLL10_COST
    assert await _get_bank_balance(session, chat_id) == bank_before + gacha_service.ROLL10_COST

    # Наивный повтор С ТЕМ ЖЕ ref_id (клиент после сетевого таймаута) не
    # должен списать стоимость повторно — debit уже идемпотентно применён
    # (ref_id занят в economy_tx), roll() обязан вернуть replay=True, не
    # трогая гранты повторно.
    monkeypatch.setattr(gacha_service, "_grant", real_grant)
    replay = await gacha_service.roll(session, chat_id, user_id, 10, ref_id)

    assert replay["replay"] is True
    assert replay["results"] == []
    assert await _get_user_balance(session, chat_id, user_id) == balance_before - gacha_service.ROLL10_COST
    assert await _get_bank_balance(session, chat_id) == bank_before + gacha_service.ROLL10_COST


# --- Float-boundary защитная ветка _weighted_choice ---------------------------


def test_weighted_choice_float_boundary_falls_back_to_last_key(monkeypatch):
    """Аудит-регрессия: `_weighted_choice` после цикла по всем тирам, если
    `point < cumulative` ни разу не сработало (point >= total из-за
    форсированного random_value=1.0), возвращает `next(reversed(weights))`
    — эта fallback-строка сама маркирует себя в коде как защиту от
    погрешности float, но ни один существующий _ForcedRng не форсировал
    random_value=1.0 (только 0.0/0.999), поэтому ветка не выполнялась ни
    разу ни в одном прогоне CI."""
    monkeypatch.setattr(gacha_service, "_rng", _ForcedRng(random_value=1.0))
    assert gacha_service._weighted_choice({"A": 0.5, "B": 0.5}) == "B"


def test_pick_tier_float_boundary_falls_back_to_last_tier(monkeypatch):
    """То же граничное значение (random_value=1.0), но через реальные
    `gacha_catalog.TIER_WEIGHTS` (сумма 1.0) и честный (не-pity) путь
    `_pick_tier` — fallback обязан вернуть именно последний ключ словаря
    ("UUR"), а не первый по частоте/произвольный тир."""
    monkeypatch.setattr(gacha_service, "_rng", _ForcedRng(random_value=1.0))
    farm = SimpleNamespace(pity_ssr=0, pity_ur=0)
    assert gacha_service._pick_tier(farm) == next(reversed(gacha_catalog.TIER_WEIGHTS)) == "UUR"


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


# --- get_collection (04.2-05: read-only, catalog-enriched) -------------------


@pytest.mark.asyncio
async def test_get_collection_empty_for_new_user(session):
    chat_id = -100910010
    user_id = 910010
    await _ensure_user(session, user_id)

    result = await gacha_service.get_collection(session, chat_id, user_id)

    assert result["characters"] == []
    assert result["pity_ssr"] == 0
    assert result["pity_ur"] == 0
    assert result["banner"] == ""


@pytest.mark.asyncio
async def test_get_collection_returns_catalog_enriched_rows(session, monkeypatch):
    chat_id = -100910011
    user_id = 910011
    await _ensure_user(session, user_id)
    await _fund(session, chat_id, user_id)

    monkeypatch.setattr(gacha_service, "_rng", _ForcedRng(random_value=0.0, choice_index=0))
    await gacha_service.roll(session, chat_id, user_id, 1, "test_get_collection_seed")

    result = await gacha_service.get_collection(session, chat_id, user_id)

    assert len(result["characters"]) == 1
    char = result["characters"][0]
    expected = gacha_catalog.chars_of_tier("S")[0]
    assert char["char_id"] == expected.char_id
    assert char["name"] == expected.name
    assert char["tier"] == "S"
    assert char["stars"] == 1
    assert char["copies"] == 1


@pytest.mark.asyncio
async def test_get_collection_reports_pity_and_banner(session, monkeypatch):
    chat_id = -100910012
    user_id = 910012
    await _ensure_user(session, user_id)
    await _fund(session, chat_id, user_id)

    banner_char = gacha_catalog.chars_of_tier("UUR")[0]
    settings_service.clear_cache()
    await settings_service.set_setting(
        session, chat_id, gacha_service.GACHA_BANNER_KEY, banner_char.char_id, updated_by_tg_id=1
    )
    await session.commit()

    monkeypatch.setattr(gacha_service, "_rng", _ForcedRng(random_value=0.0, choice_index=0))
    await gacha_service.roll(session, chat_id, user_id, 1, "test_get_collection_pity_seed")

    result = await gacha_service.get_collection(session, chat_id, user_id)

    assert result["pity_ssr"] == 1  # S-roll increments both counters (D-03)
    assert result["pity_ur"] == 1
    assert result["banner"] == banner_char.char_id

    settings_service.clear_cache()


@pytest.mark.asyncio
async def test_get_collection_reports_const_level_and_art_slug(session, monkeypatch):
    """GACHA-04: каждый персонаж в get_collection несёт const_level
    (`constellation_catalog.const_level(copies)`, min(6, copies-1)) и
    art_slug (статичный ассет Mini App из `gacha_catalog.CATALOG`) —
    доказываем на персонаже с несколькими дублями (copies=3), где
    const_level уже не тривиальный 0."""
    chat_id = -100910013
    user_id = 910013
    await _ensure_user(session, user_id)
    await _fund(session, chat_id, user_id)
    await _top_up(session, chat_id, user_id, 10_000, "test_const_level_top_up")

    monkeypatch.setattr(gacha_service, "_rng", _ForcedRng(random_value=0.0, choice_index=0))
    char = gacha_catalog.chars_of_tier("S")[0]

    for i in range(1, 4):
        await gacha_service.roll(session, chat_id, user_id, 1, f"test_const_level_{i}")

    result = await gacha_service.get_collection(session, chat_id, user_id)

    assert len(result["characters"]) == 1
    entry = result["characters"][0]
    assert entry["char_id"] == char.char_id
    assert entry["copies"] == 3
    assert entry["const_level"] == constellation_catalog.const_level(3)
    assert entry["const_level"] == 2  # min(6, 3-1)
    assert entry["art_slug"]
    assert entry["art_slug"] == gacha_catalog.CATALOG[char.char_id].art_slug


@pytest.mark.asyncio
async def test_get_collection_roster_covers_full_catalog_with_owned_flag(session, monkeypatch):
    """GACHA-04: `roster` (в отличие от `characters`) всегда содержит ВЕСЬ
    каталог из 15 героинь, с `owned=False`/нулевыми stars/copies/const_level
    для ещё не собранных — экран коллекции Mini App рисует их как
    заблокированные карточки вместо пустого текста-заглушки."""
    chat_id = -100910014
    user_id = 910014
    await _ensure_user(session, user_id)
    await _fund(session, chat_id, user_id)

    monkeypatch.setattr(gacha_service, "_rng", _ForcedRng(random_value=0.0, choice_index=0))
    await gacha_service.roll(session, chat_id, user_id, 1, "test_roster_seed")

    result = await gacha_service.get_collection(session, chat_id, user_id)
    granted_char_id = result["characters"][0]["char_id"]

    assert len(result["roster"]) == len(gacha_catalog.CATALOG)
    roster_by_id = {c["char_id"] for c in result["roster"]}
    assert roster_by_id == set(gacha_catalog.CATALOG.keys())

    granted_entry = next(c for c in result["roster"] if c["char_id"] == granted_char_id)
    assert granted_entry["owned"] is True
    assert granted_entry["stars"] == 1
    assert granted_entry["copies"] == 1

    for entry in result["roster"]:
        if entry["char_id"] == granted_char_id:
            continue
        assert entry["owned"] is False
        assert entry["stars"] == 0
        assert entry["copies"] == 0
        assert entry["const_level"] == 0
        assert entry["art_slug"] == gacha_catalog.CATALOG[entry["char_id"]].art_slug


# --- get_banner_info (design-хендофф §5: pre-roll read) ----------------------


@pytest.mark.asyncio
async def test_get_banner_info_fresh_user_reports_defaults(session):
    chat_id = -100910014
    user_id = 910014
    await _ensure_user(session, user_id)

    result = await gacha_service.get_banner_info(session, chat_id, user_id)

    assert "featured_id" in result
    assert "rates" in result
    assert "pity_ssr" in result
    assert "pity_ur" in result
    assert "cost_single" in result
    assert "cost_ten" in result
    assert result["pity_ssr"] == 0  # свежая ферма — pity ещё не копился
    assert result["pity_ur"] == 0
    assert result["cost_single"] == gacha_service.ROLL_COST
    assert result["cost_ten"] == gacha_service.ROLL10_COST


@pytest.mark.asyncio
async def test_get_banner_info_falls_back_to_showcase_uur_when_unset(session):
    """Без настроенного gacha_banner (BotSetting) featured_* не пустые —
    подставляется первый UUR каталога как "витрина" с is_rate_up=False, так
    что хаб/герой-баннер /gacha никогда не остаются без арта в новом чате
    (см. докстринг get_banner_info)."""
    chat_id = -100910020
    user_id = 910020
    await _ensure_user(session, user_id)

    result = await gacha_service.get_banner_info(session, chat_id, user_id)

    showcase = next(c for c in gacha_catalog.CATALOG.values() if c.tier == "UUR")
    assert result["is_rate_up"] is False
    assert result["featured_id"] == showcase.char_id
    assert result["featured_name"] == showcase.name
    assert result["featured_tier"] == "UUR"
    assert result["featured_art_slug"] == showcase.art_slug


@pytest.mark.asyncio
async def test_get_banner_info_reports_real_rate_up_when_configured(session):
    chat_id = -100910021
    user_id = 910021
    await _ensure_user(session, user_id)

    banner_char = gacha_catalog.chars_of_tier("UUR")[-1]
    settings_service.clear_cache()
    await settings_service.set_setting(
        session, chat_id, gacha_service.GACHA_BANNER_KEY, banner_char.char_id, updated_by_tg_id=1
    )
    await session.commit()

    result = await gacha_service.get_banner_info(session, chat_id, user_id)

    assert result["is_rate_up"] is True
    assert result["featured_id"] == banner_char.char_id
    assert result["featured_name"] == banner_char.name
    assert result["featured_art_slug"] == banner_char.art_slug

    settings_service.clear_cache()
