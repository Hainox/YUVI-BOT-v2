"""Жизненный цикл автобитв (GACHA-06) — PvP на ставку, где исход взвешен
суммарной "силой" гача-коллекции каждой стороны, а не честным 50/50 (в
отличие от Duel, читай `bot/services/duel_service.py`'s докстринг — эта
секция построена буквально "по образцу" него: тот же эскроу/резолв/рефанд
через `economy_service`, тот же RNG-seam `_rng`, тот же контракт порядка
блокировок (строка сначала, деньги потом), та же идемпотентность через
ref_id/статус-переход. Расписываю здесь ТОЛЬКО то, что реально отличается —
за остальным читай duel_service.py.

Деньги двигает ТОЛЬКО через `bot.services.economy_service` — этот модуль
никогда не пишет user_balance/chat_bank/economy_tx напрямую.

Сила команды (`team_power`) НЕ хранится персистентно нигде — пересчитывается
из живого `GachaCollection` на каждый вызов `create_autobattle`/
`accept_autobattle`. Это намеренно: коллекция может вырасти (роллы,
апгрейды фермы) между вызовом и приёмом, и сила ОБЕИХ сторон должна быть
актуальной на момент реального резолва, а не замороженной на момент вызова
challenger'а. `AutoBattle.challenger_power`/`opponent_power` — снапшот на
момент `accept_autobattle` (когда бой реально резолвится), только для
отображения результата/аудита.

Формула силы персонажа — НОВАЯ (в кодовой базе не было готового "боевого"
поля силы, экономические тир-ставки `clicker_service.WORKER_TIER_CP_PER_SEC`
специально не переиспользуются — те посчитаны под доход фермы, не под PvP):
`TIER_POWER[tier] * star_mult(stars) * (1 + FARM_LEVEL_POWER_PCT*(farm_level-1))`,
`star_mult` — тот же самый `gacha_catalog.star_mult`, что уже использует
ферма (не дублируем формулу второй раз), сумма по всем собранным персонажам.

Вероятность победы (`win_probability`) — НЕ честная монетка: пропорциональна
силе (`power_a / (power_a + power_b)`), но зажата в [MIN_WIN_PROBABILITY,
MAX_WIN_PROBABILITY] — сильная коллекция даёт реальное преимущество, но
никогда не гарантирует исход (иначе это была бы не игра, а публичный
рейтинг коллекций). Вся арифметика — Decimal от первого до последнего шага
(дайс-урок 2026-08-06, см. докстринг casino_service.dice_fair_payout) —
единственный float-источник, `_rng.random()`, оборачивается в Decimal СРАЗУ
на границе (`Decimal(_rng.random())`), той же формой, что
`crash_engine.generate_crash_point`.
"""

from __future__ import annotations

import logging
import math
import secrets
from datetime import datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import settings
from bot.services import economy_service
from bot.services import gacha_catalog
from common.models.autobattle import AutoBattle
from common.models.gacha_collection import GachaCollection

logger = logging.getLogger(__name__)

_rng = secrets.SystemRandom()

# --- Сила команды (GACHA-06, новая формула — нет готового аналога в базе) ---

TIER_POWER: dict[str, Decimal] = {
    "R": Decimal(10),
    "S": Decimal(25),
    "UR": Decimal(60),
    "UUR": Decimal(150),
}
# +1%/уровень фермы персонажа (макс. уровень 50, clicker_service.FARM_LEVEL_MAX)
# -> до +49% силы на полностью прокачанном персонаже.
FARM_LEVEL_POWER_PCT = Decimal("0.01")

# Вероятность победы никогда не выходит за эти границы — даже многократный
# перевес по силе не гарантирует исход, см. модульный докстринг.
MIN_WIN_PROBABILITY = Decimal("0.05")
MAX_WIN_PROBABILITY = Decimal("0.95")


def _character_power(tier: str, stars: int, farm_level: int) -> Decimal:
    """Сила ОДНОГО собранного персонажа. `star_mult` (gacha_catalog.py)
    возвращает `float`, но для stars в диапазоне 1..5 (gacha_catalog.MAX_STARS)
    результат — точная двоичная дробь (1.0/1.25/1.5/1.75/2.0), поэтому
    `Decimal(str(...))` здесь не теряет точность (не общий случай
    float->Decimal, а конкретно безопасный для этого узкого домена)."""
    base = TIER_POWER.get(tier, Decimal(0))
    star_bonus = Decimal(str(gacha_catalog.star_mult(stars)))
    farm_bonus = Decimal(1) + FARM_LEVEL_POWER_PCT * Decimal(farm_level - 1)
    return base * star_bonus * farm_bonus


async def team_power(session: AsyncSession, chat_id: int, user_id: int) -> int:
    """Суммарная сила ВСЕЙ собранной гача-коллекции игрока в этом чате —
    чистое чтение, ничего не пишет. `int(...)` округляет вниз (Decimal ->
    int усечением), та же дисциплина "не переплатить дробный остаток", что
    everywhere в проекте платит `int()` от Decimal (см. casino_service).
    Персонаж без записи в каталоге (не должно случаться, но не должно и
    падать) пропускается — тот же defensive skip, что gacha_service.
    get_collection делает через `CATALOG.get(...)`."""
    rows = (
        await session.execute(
            select(GachaCollection).where(
                GachaCollection.chat_id == chat_id,
                GachaCollection.user_id == user_id,
            )
        )
    ).scalars().all()

    total = Decimal(0)
    for row in rows:
        char = gacha_catalog.CATALOG.get(row.char_id)
        if char is None:
            continue
        total += _character_power(char.tier, row.stars, row.farm_level)
    return int(total)


def win_probability(power_a: int, power_b: int) -> Decimal:
    """P(сторона A побеждает) — пропорционально силе, зажато в
    [MIN_WIN_PROBABILITY, MAX_WIN_PROBABILITY]. `power_a == power_b == 0`
    (обе стороны без единого персонажа) -> честные 0.5, ни у кого нет
    преимущества по силе, которого нет."""
    total = power_a + power_b
    if total == 0:
        return Decimal("0.5")
    raw = Decimal(power_a) / Decimal(total)
    return max(MIN_WIN_PROBABILITY, min(MAX_WIN_PROBABILITY, raw))


# --- Исключения ---------------------------------------------------------


class AutoBattleError(Exception):
    """Базовое исключение модуля автобитв."""


class AutoBattleNotFound(AutoBattleError):
    """Автобитва с указанным id не найдена в этом чате."""


class AutoBattleAlreadyResolved(AutoBattleError):
    """Повторный запрос на уже обработанную (не-pending) автобитву."""


# --- Валидация ставки (тот же порог, что casino/Duel) ------------------------


def _validate_stake(stake: int, balance: int) -> None:
    if stake < settings.casino_min_bet:
        raise AutoBattleError(f"Минимальная ставка — {settings.casino_min_bet} ювиков")
    max_stake = int(balance * settings.casino_max_bet_pct)
    if stake > max_stake:
        raise AutoBattleError(f"Максимальная ставка — {max_stake} ювиков (баланс {balance})")


def _fee_and_pot(stake: int) -> tuple[int, int]:
    """Та же формула, что duel_service._fee_and_pot: fee = max(1,
    ceil(2*stake*transfer_fee_pct)); pot = 2*stake - fee."""
    pot_total = 2 * stake
    fee = max(1, math.ceil(pot_total * settings.transfer_fee_pct))
    return fee, pot_total - fee


async def _escrow_stake(
    session: AsyncSession, chat_id: int, user_id: int, stake: int, ref_id: str
) -> bool:
    return await economy_service.debit_to_bank(
        session, chat_id, user_id, stake, kind="autobattle_stake", ref_id=ref_id
    )


async def _get_autobattle_for_update(session: AsyncSession, chat_id: int, autobattle_id: int) -> AutoBattle:
    battle = (
        await session.execute(
            select(AutoBattle)
            .where(AutoBattle.chat_id == chat_id, AutoBattle.id == autobattle_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if battle is None:
        raise AutoBattleNotFound(f"Автобитва #{autobattle_id} не найдена")
    return battle


def _resolved_result(battle: AutoBattle) -> dict:
    pot = 2 * battle.stake - battle.fee if battle.fee is not None else 0
    return {
        "status": battle.status,
        "autobattle_id": battle.id,
        "winner_id": battle.winner_id,
        "loser_id": battle.loser_id,
        "challenger_power": battle.challenger_power,
        "opponent_power": battle.opponent_power,
        "fee": battle.fee,
        "pot": pot,
    }


# --- create_autobattle (эскроу ставки челленджера) ---------------------------


async def create_autobattle(
    session: AsyncSession,
    chat_id: int,
    challenger_id: int,
    opponent_id: int,
    stake: int,
    ref_id: str,
) -> AutoBattle:
    """Создаёт вызов на автобитву — та же форма, что duel_service.
    create_duel (читай его докстринг за полным разбором WR-01/WR-04:
    self-battle гард здесь же в сервисе, не только в хендлере; ставка
    эскроуируется у челленджера, но НЕ заходит в chat_bank до реального
    резолва в accept_autobattle — рефанд отменённого/отклонённого вызова
    не зависит от bank-cap D-06)."""
    if opponent_id == challenger_id:
        raise AutoBattleError("Нельзя вызвать на автобитву самого себя")

    balance = await economy_service.get_balance(session, chat_id, challenger_id)
    _validate_stake(stake, balance)

    escrowed = await economy_service.debit(
        session, chat_id, challenger_id, stake, kind="autobattle_stake", ref_id=ref_id
    )
    if not escrowed:
        logger.info("create_autobattle: ref_id=%s уже обработан, пропускаем", ref_id)
        raise AutoBattleAlreadyResolved(f"Запрос на автобитву уже обработан (ref_id={ref_id})")

    battle = AutoBattle(
        chat_id=chat_id,
        challenger_id=challenger_id,
        opponent_id=opponent_id,
        stake=stake,
        status="pending",
    )
    session.add(battle)
    await session.commit()
    return battle


# --- accept_autobattle (сила -> взвешенная монетка + fee) -------------------


async def accept_autobattle(
    session: AsyncSession, chat_id: int, autobattle_id: int, opponent_id: int, ref_id: str
) -> dict:
    """Принимает вызов и резолвит бой синхронно — та же форма, что
    duel_service.accept_duel (FOR UPDATE строки первым, статус-переход
    "pending"->"resolved" сам служит гардом идемпотентности, peek_balance
    вместо get_balance, чтобы не разрывать лок строки). Единственное
    отличие механики от Duel: вместо честного `_rng.choice([...])`
    считается сила обеих команд (`team_power`) ПРЯМО СЕЙЧАС (актуальная на
    момент боя, см. модульный докстринг) и монетка взвешена
    `win_probability`."""
    battle = await _get_autobattle_for_update(session, chat_id, autobattle_id)
    if battle.status != "pending":
        await session.commit()
        return _resolved_result(battle)
    if battle.opponent_id != opponent_id:
        await session.commit()
        raise AutoBattleError("Только приглашённый оппонент может принять автобитву")

    balance = await economy_service.peek_balance(session, chat_id, opponent_id)
    _validate_stake(battle.stake, balance)

    escrowed = await _escrow_stake(session, chat_id, opponent_id, battle.stake, ref_id)
    if not escrowed:
        raise AutoBattleAlreadyResolved(f"Запрос на приём автобитвы уже обработан (ref_id={ref_id})")

    await economy_service.credit_bank(
        session,
        chat_id,
        battle.stake,
        kind="autobattle_stake",
        ref_id=f"autobattle:{autobattle_id}:challenger_bank",
    )

    challenger_power = await team_power(session, chat_id, battle.challenger_id)
    opponent_power = await team_power(session, chat_id, opponent_id)
    p_challenger_wins = win_probability(challenger_power, opponent_power)

    r = Decimal(_rng.random())
    challenger_won = r < p_challenger_wins
    winner_id = battle.challenger_id if challenger_won else opponent_id
    loser_id = opponent_id if challenger_won else battle.challenger_id

    fee, pot = _fee_and_pot(battle.stake)
    paid = await economy_service.pay_from_bank(
        session, chat_id, winner_id, pot, kind="autobattle_payout", ref_id=f"autobattle:{autobattle_id}:payout"
    )

    battle.winner_id = winner_id
    battle.loser_id = loser_id
    battle.challenger_power = challenger_power
    battle.opponent_power = opponent_power
    battle.fee = fee
    battle.status = "resolved"
    battle.resolved_at = datetime.utcnow()
    await session.commit()

    return {
        "status": "resolved",
        "autobattle_id": autobattle_id,
        "winner_id": winner_id,
        "loser_id": loser_id,
        "challenger_power": challenger_power,
        "opponent_power": opponent_power,
        "fee": fee,
        "pot": paid,
    }


# --- decline_autobattle / cancel_autobattle (полный рефанд) -----------------


async def _refund_pending_autobattle(
    session: AsyncSession, chat_id: int, autobattle_id: int, actor_id: int, actor_field: str, new_status: str
) -> dict:
    """Та же форма, что duel_service._refund_pending_duel — прямой
    `economy_service.credit` (не `pay_from_bank`), ставка челленджера
    никогда не заходила в chat_bank, это его собственные деньги, полный
    рефанд не зависит от текущего остатка банка."""
    battle = await _get_autobattle_for_update(session, chat_id, autobattle_id)
    if battle.status != "pending":
        await session.commit()
        return {"status": battle.status, "autobattle_id": autobattle_id, "refunded": 0}

    if getattr(battle, actor_field) != actor_id:
        await session.commit()
        raise AutoBattleError("Только соответствующий участник автобитвы может выполнить это действие")

    refunded_ok = await economy_service.credit(
        session,
        chat_id,
        battle.challenger_id,
        battle.stake,
        kind="autobattle_refund",
        ref_id=f"autobattle:{autobattle_id}:refund",
    )
    battle.status = new_status
    await session.commit()
    return {"status": new_status, "autobattle_id": autobattle_id, "refunded": battle.stake if refunded_ok else 0}


async def decline_autobattle(session: AsyncSession, chat_id: int, autobattle_id: int, actor_id: int) -> dict:
    """Отклоняет pending-автобитву (только приглашённый оппонент) — полный
    рефанд ставки челленджеру."""
    return await _refund_pending_autobattle(
        session, chat_id, autobattle_id, actor_id, "opponent_id", "declined"
    )


async def cancel_autobattle(session: AsyncSession, chat_id: int, autobattle_id: int, actor_id: int) -> dict:
    """Отменяет pending-автобитву (только челленджер) — полный рефанд ставки."""
    return await _refund_pending_autobattle(
        session, chat_id, autobattle_id, actor_id, "challenger_id", "cancelled"
    )
