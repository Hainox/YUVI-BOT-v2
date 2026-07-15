"""Интеграционные тесты AMM-моста CP<->ювик (clicker_service, план 04.1-05)
против живого Postgres (фикстура `session` из tests/conftest.py —
транзакция-на-тест). Доказывают D-03 (`04-CONTEXT.md`):

- constant-product ценообразование (`r_cp * r_h = k`) со встроенным
  слиппеджем (крупная конвертация даёт худший курс за единицу CP);
- `convert_cp`/`buy_cp` двигают ювики ИСКЛЮЧИТЕЛЬНО через economy_service
  (mint при convert, sink в банк при buy) — сам AMM никогда не пишет
  user_balance/chat_bank/economy_tx;
- mean-reversion тик (`amm_tick`) раз в ~10 минут тянет цену к якорю
  (100 CP/ювик) множителем `exp(-TICK/TAU)` и пишет снапшот цены;
- идемпотентность по ref_id на стороне ювиков (economy_service), как и у
  всех остальных денежных операций проекта (markets_service/economy_service).

clicker_service сам делает session.commit() там, где это описано в его
контракте — совместимо с фикстурой session благодаря join-savepoint режиму
SQLAlchemy 2.0 (тот же паттерн уже проверен в test_clicker_service.py/
test_markets_service.py).
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy import update

from bot.services import clicker_service
from bot.services import economy_service
from common.models.clicker_farm import ClickerFarm
from common.models.clicker_market_pool import ClickerMarketPool
from common.models.clicker_market_price import ClickerMarketPrice
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


async def _seed_farm_cp(session, chat_id: int, user_id: int, cp: int) -> None:
    """Заводит строку фермы (get-or-create через сам сервис) и напрямую
    выставляет CP в обход тапов/апгрейдов — детерминированная подготовка."""
    await clicker_service.get_farm_state(session, chat_id, user_id)
    await session.execute(
        update(ClickerFarm)
        .where(ClickerFarm.chat_id == chat_id, ClickerFarm.user_id == user_id)
        .values(cp=cp)
    )
    await session.commit()
    session.expire_all()


async def _get_farm_cp(session, chat_id: int, user_id: int) -> int:
    result = await session.execute(
        select(ClickerFarm.cp).where(
            ClickerFarm.chat_id == chat_id, ClickerFarm.user_id == user_id
        )
    )
    return result.scalar_one()


async def _get_pool(session, chat_id: int) -> ClickerMarketPool:
    result = await session.execute(
        select(ClickerMarketPool).where(ClickerMarketPool.chat_id == chat_id)
    )
    return result.scalar_one()


async def _set_pool_reserves(session, chat_id: int, r_cp: Decimal, r_h: Decimal) -> None:
    await session.execute(
        update(ClickerMarketPool)
        .where(ClickerMarketPool.chat_id == chat_id)
        .values(r_cp=r_cp, r_h=r_h)
    )
    await session.commit()
    session.expire_all()


async def _count_price_snapshots(session, chat_id: int) -> int:
    result = await session.execute(
        select(ClickerMarketPrice).where(ClickerMarketPrice.chat_id == chat_id)
    )
    return len(result.scalars().all())


# --- get_market_state (get-or-create пула) --------------------------------


@pytest.mark.asyncio
async def test_pool_seeded_on_first_access(session):
    chat_id = -100920001

    state = await clicker_service.get_market_state(session, chat_id)

    pool = await _get_pool(session, chat_id)
    assert pool.r_cp == Decimal(clicker_service.AMM_SEED_R_CP)
    assert pool.r_h == Decimal(clicker_service.AMM_SEED_R_H)
    assert state["price"] == pytest.approx(
        float(clicker_service.AMM_ANCHOR_CP_PER_HRYVNA), rel=1e-9
    )


# --- convert_cp (CP -> ювик, mint) -----------------------------------------


@pytest.mark.asyncio
async def test_convert_reduces_cp_and_credits_hryvnia(session):
    chat_id = -100920002
    user_id = 920002
    await _ensure_user(session, user_id)
    balance_before = await _fund(session, chat_id, user_id)
    await _seed_farm_cp(session, chat_id, user_id, cp=1_000_000)

    result = await clicker_service.convert_cp(
        session, chat_id, user_id, cp_in=200_000, ref_id="convert:test:1"
    )

    assert result["hryvnia_out"] > 0

    farm_cp = await _get_farm_cp(session, chat_id, user_id)
    assert farm_cp == 1_000_000 - 200_000

    balance_after = await _get_user_balance(session, chat_id, user_id)
    assert balance_after == balance_before + result["hryvnia_out"]


# --- buy_cp (ювик -> CP, sink в банк) --------------------------------------


@pytest.mark.asyncio
async def test_buy_debits_hryvnia_and_adds_cp(session):
    chat_id = -100920003
    user_id = 920003
    await _ensure_user(session, user_id)
    balance_before = await _fund(session, chat_id, user_id)
    await _seed_farm_cp(session, chat_id, user_id, cp=0)

    hryvnia_in = 100
    result = await clicker_service.buy_cp(
        session, chat_id, user_id, hryvnia_in=hryvnia_in, ref_id="buy:test:1"
    )

    assert result["cp_out"] > 0

    balance_after = await _get_user_balance(session, chat_id, user_id)
    assert balance_after == balance_before - hryvnia_in

    farm_cp = await _get_farm_cp(session, chat_id, user_id)
    assert farm_cp == result["cp_out"]


# --- constant-product invariant + slippage ---------------------------------


@pytest.mark.asyncio
async def test_constant_product_invariant(session):
    chat_id = -100920004
    await clicker_service.get_market_state(session, chat_id)  # seeds the pool row
    pool = await _get_pool(session, chat_id)

    k = pool.r_cp * pool.r_h

    small_out, small_new_r_cp, small_new_r_h = clicker_service.quote_convert(pool, 200_000)
    large_out, large_new_r_cp, large_new_r_h = clicker_service.quote_convert(pool, 2_000_000)

    # Инвариант: после свопа r_cp*r_h всё ещё ~= k (допуск под округление
    # Numeric(20,8) — выход всегда floor'ится, так что new_k может быть чуть
    # БОЛЬШЕ k, но не меньше и не более чем на пренебрежимо малую дельту).
    new_k_small = small_new_r_cp * small_new_r_h
    assert new_k_small >= k
    assert (new_k_small - k) / k < Decimal("0.000001")

    # Слиппедж: более крупная конвертация даёт худший курс за единицу CP.
    small_rate = Decimal(small_out) / Decimal(200_000)
    large_rate = Decimal(large_out) / Decimal(2_000_000)
    assert large_rate < small_rate


# --- Отклонение при нехватке средств ----------------------------------------


@pytest.mark.asyncio
async def test_convert_insufficient_cp_rejected(session):
    chat_id = -100920005
    user_id = 920005
    await _ensure_user(session, user_id)
    balance_before = await _fund(session, chat_id, user_id)
    await _seed_farm_cp(session, chat_id, user_id, cp=100)

    with pytest.raises(clicker_service.ClickerError):
        await clicker_service.convert_cp(
            session, chat_id, user_id, cp_in=1_000, ref_id="convert:test:reject"
        )

    balance_after = await _get_user_balance(session, chat_id, user_id)
    assert balance_after == balance_before

    farm_cp = await _get_farm_cp(session, chat_id, user_id)
    assert farm_cp == 100


@pytest.mark.asyncio
async def test_buy_insufficient_hryvnia_rejected(session):
    chat_id = -100920006
    user_id = 920006
    await _ensure_user(session, user_id)
    balance_before = await _fund(session, chat_id, user_id)  # старт-бонус (economy_start_bonus)
    await _seed_farm_cp(session, chat_id, user_id, cp=0)

    with pytest.raises(economy_service.InsufficientFunds):
        await clicker_service.buy_cp(
            session,
            chat_id,
            user_id,
            hryvnia_in=balance_before + 1,
            ref_id="buy:test:reject",
        )

    balance_after = await _get_user_balance(session, chat_id, user_id)
    assert balance_after == balance_before

    farm_cp = await _get_farm_cp(session, chat_id, user_id)
    assert farm_cp == 0


# --- mean-reversion тик -----------------------------------------------------


@pytest.mark.asyncio
async def test_tick_mean_reverts_toward_anchor(session):
    chat_id = -100920007
    await clicker_service.get_market_state(session, chat_id)  # seeds the pool row

    # Скашиваем пул далеко от якоря (100 CP/ювик) в сторону "CP дороже".
    skewed_r_cp = Decimal(clicker_service.AMM_SEED_R_CP) * 2
    skewed_r_h = Decimal(clicker_service.AMM_SEED_R_H)
    await _set_pool_reserves(session, chat_id, skewed_r_cp, skewed_r_h)

    price_before = skewed_r_cp / skewed_r_h
    anchor = Decimal(clicker_service.AMM_ANCHOR_CP_PER_HRYVNA)
    assert price_before > anchor  # sanity: пул реально скошен выше якоря

    ticked = await clicker_service.amm_tick(session)
    assert ticked >= 1

    pool = await _get_pool(session, chat_id)
    price_after = pool.r_cp / pool.r_h

    # Цена сдвинулась В СТОРОНУ якоря (не обязательно достигла его за 1 тик).
    assert anchor < price_after < price_before

    snapshot_count = await _count_price_snapshots(session, chat_id)
    assert snapshot_count >= 1


# --- Идемпотентность по ref_id (economy_service) ----------------------------


@pytest.mark.asyncio
async def test_swap_is_idempotent_on_ref_id(session):
    chat_id = -100920008
    user_id = 920008
    await _ensure_user(session, user_id)
    await _fund(session, chat_id, user_id)
    await _seed_farm_cp(session, chat_id, user_id, cp=1_000_000)

    ref_id = "convert:test:idempotent"
    first = await clicker_service.convert_cp(session, chat_id, user_id, cp_in=100_000, ref_id=ref_id)
    balance_after_first = await _get_user_balance(session, chat_id, user_id)
    farm_cp_after_first = await _get_farm_cp(session, chat_id, user_id)

    second = await clicker_service.convert_cp(session, chat_id, user_id, cp_in=100_000, ref_id=ref_id)
    balance_after_second = await _get_user_balance(session, chat_id, user_id)
    farm_cp_after_second = await _get_farm_cp(session, chat_id, user_id)

    assert first["hryvnia_out"] > 0
    # Повтор с тем же ref_id — ювики НЕ двигаются повторно.
    assert balance_after_second == balance_after_first
    # CP фермы тоже не списывается повторно (credit-first-then-mutate).
    assert farm_cp_after_second == farm_cp_after_first
    assert second.get("status") == "duplicate"
