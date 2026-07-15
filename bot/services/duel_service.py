"""Жизненный цикл дуэлей (DUEL-01) — /duel (PvP), /duelbot (против банка чата),
приём/отклонение/отмена вызова.

Деньги двигает ТОЛЬКО через `bot.services.economy_service` (debit/credit_bank
для эскроу ставок, pay_from_bank для выплаты победителю) — этот модуль
НИКОГДА не пишет user_balance/chat_bank/economy_tx напрямую
(economy_service.py — единственный модуль с таким правом, см. его докстринг).

Идемпотентность:
- create_duel/duelbot — одноразовая операция, ref_id передаётся вызывающим
  (обычно производный от Telegram-апдейта); повтор с тем же ref_id ловится
  economy_service.debit (возвращает False) и поднимает DuelAlreadyResolved
  (та же практика, что markets_service.create_market: не искать/возвращать
  уже созданную дуэль, а явно сообщить о повторе).
- accept_duel/decline_duel/cancel_duel — статус-переход дуэли (pending ->
  accepted/resolved/declined/cancelled) САМ служит гардом идемпотентности
  (форма markets_service.resolve_market/cancel_market): повторный вызов на
  уже нерending-дуэли — no-op, деньги не двигаются повторно.

Контракт порядка блокировок: строка Duel блокируется FOR UPDATE ПЕРВОЙ
(замораживает status), затем движутся деньги — та же форма, что
markets_service.place_bet/resolve_market (market-row-first).

Исход дуэли — ТОЛЬКО через `secrets.SystemRandom()` (модульный RNG-seam
`_rng`, подменяется в тестах monkeypatch'ем) — server-authoritative,
клиент НИКОГДА не поставляет исход раунда (D-03/T-04.1-24).
"""

from __future__ import annotations

import logging
import math
import secrets
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import settings
from bot.services import economy_service
from common.models.duel import Duel

logger = logging.getLogger(__name__)

_rng = secrets.SystemRandom()

# D-01: фиксированная длительность мута проигравшего, независимо от ставки.
MUTE_SECONDS = 600

# opponent_id NULL == дуэль против банка чата (/duelbot).
BANK_SENTINEL = None


# --- Исключения ---------------------------------------------------------


class DuelError(Exception):
    """Базовое исключение модуля дуэлей."""


class DuelNotFound(DuelError):
    """Дуэль с указанным id не найдена в этом чате."""


class DuelAlreadyResolved(DuelError):
    """Повторный запрос на уже обработанную (не-pending) дуэль."""


# --- Валидация ставки (D-04/D-05, тот же порог, что казино) -----------------


def _validate_stake(stake: int, balance: int) -> None:
    """D-04: единая минимальная ставка для всех игр казино и дуэлей.
    D-05: максимум — % от текущего баланса (по умолчанию 100%)."""
    if stake < settings.casino_min_bet:
        raise DuelError(f"Минимальная ставка — {settings.casino_min_bet} ювиков")
    max_stake = int(balance * settings.casino_max_bet_pct)
    if stake > max_stake:
        raise DuelError(f"Максимальная ставка — {max_stake} ювиков (баланс {balance})")


def _fee_and_pot(stake: int) -> tuple[int, int]:
    """D-04: fee = max(1, ceil(2*stake*transfer_fee_pct)); pot = 2*stake - fee."""
    pot_total = 2 * stake
    fee = max(1, math.ceil(pot_total * settings.transfer_fee_pct))
    return fee, pot_total - fee


async def _escrow_stake(
    session: AsyncSession, chat_id: int, user_id: int, stake: int, ref_id: str
) -> bool:
    """Списывает ставку с игрока и зачисляет её в банк чата — общий эскроу
    для create_duel/accept_duel/duelbot. Возвращает False, если ref_id уже
    применялся (replay)."""
    debited = await economy_service.debit(
        session, chat_id, user_id, stake, kind="duel_stake", ref_id=ref_id
    )
    if not debited:
        return False
    await economy_service.credit_bank(
        session, chat_id, stake, kind="duel_stake", ref_id=f"{ref_id}:bank"
    )
    return True


async def _get_duel_for_update(session: AsyncSession, chat_id: int, duel_id: int) -> Duel:
    duel = (
        await session.execute(
            select(Duel).where(Duel.chat_id == chat_id, Duel.id == duel_id).with_for_update()
        )
    ).scalar_one_or_none()
    if duel is None:
        raise DuelNotFound(f"Дуэль #{duel_id} не найдена")
    return duel


def _resolved_result(duel: Duel) -> dict:
    """Восстанавливает форму результата accept_duel из уже resolved-строки
    (гард идемпотентности) — pot пересчитывается из stake/fee, т.к. эскроу
    всегда покрывает pot целиком (bank-cap D-06 здесь не задействуется)."""
    pot = 2 * duel.stake - duel.fee if duel.fee is not None else 0
    return {
        "status": duel.status,
        "duel_id": duel.id,
        "winner_id": duel.winner_id,
        "loser_id": duel.loser_id,
        "fee": duel.fee,
        "pot": pot,
        "mute_seconds": duel.mute_seconds,
    }


# --- create_duel (эскроу ставки челленджера) --------------------------------


async def create_duel(
    session: AsyncSession,
    chat_id: int,
    challenger_id: int,
    opponent_id: int,
    stake: int,
    ref_id: str,
) -> Duel:
    """Создаёт вызов на дуэль: валидирует ставку против баланса челленджера,
    эскроу ставки в банк чата, вставляет Duel(status="pending"). Поднимает
    DuelError при нарушении лимитов D-04/D-05, economy_service.InsufficientFunds
    при нехватке средств, DuelAlreadyResolved при повторе ref_id (идемпотентный
    no-op — дуэль повторно не создаётся, форма markets_service.create_market)."""
    balance = await economy_service.get_balance(session, chat_id, challenger_id)
    _validate_stake(stake, balance)

    escrowed = await _escrow_stake(session, chat_id, challenger_id, stake, ref_id)
    if not escrowed:
        logger.info("create_duel: ref_id=%s уже обработан, пропускаем", ref_id)
        raise DuelAlreadyResolved(f"Запрос на дуэль уже обработан (ref_id={ref_id})")

    duel = Duel(
        chat_id=chat_id,
        challenger_id=challenger_id,
        opponent_id=opponent_id,
        stake=stake,
        status="pending",
        mute_seconds=MUTE_SECONDS,
    )
    session.add(duel)
    await session.commit()
    return duel


# --- accept_duel (coinflip + 5% fee) -----------------------------------------


async def accept_duel(
    session: AsyncSession, chat_id: int, duel_id: int, opponent_id: int, ref_id: str
) -> dict:
    """Принимает вызов: `SELECT Duel ... FOR UPDATE` первым (контракт порядка
    блокировок) — статус-переход "pending"->"resolved" сам служит гардом
    идемпотентности (повторный вызов на уже resolved-дуэли — no-op, деньги не
    двигаются повторно, T-04.1-25). Эскроу ставки оппонента, комиссия
    fee = max(1, ceil(2*stake*transfer_fee_pct)) (D-04), coinflip победителя
    через RNG-seam `_rng` (server-authoritative), выплата победителю через
    economy_service.pay_from_bank (D-06 bank-cap)."""
    duel = await _get_duel_for_update(session, chat_id, duel_id)
    if duel.status != "pending":
        await session.commit()
        return _resolved_result(duel)
    if duel.opponent_id != opponent_id:
        await session.commit()
        raise DuelError("Только приглашённый оппонент может принять дуэль")

    # peek_balance (не commit!) — иначе разрывает FOR UPDATE выше и открывает
    # окно гонки, где два параллельных accept_duel оба проходят проверку
    # status == "pending" и оба эскроуируют ставку (двойное списание).
    balance = await economy_service.peek_balance(session, chat_id, opponent_id)
    _validate_stake(duel.stake, balance)

    escrowed = await _escrow_stake(session, chat_id, opponent_id, duel.stake, ref_id)
    if not escrowed:
        # Гонка: ref_id уже применён, но статус дуэли ещё "pending" в этой
        # транзакции быть не должно (FOR UPDATE сериализует конкурентные
        # accept) — защитный backstop.
        raise DuelAlreadyResolved(f"Запрос на приём дуэли уже обработан (ref_id={ref_id})")

    fee, pot = _fee_and_pot(duel.stake)
    winner_id = _rng.choice([duel.challenger_id, opponent_id])
    loser_id = opponent_id if winner_id == duel.challenger_id else duel.challenger_id

    paid = await economy_service.pay_from_bank(
        session, chat_id, winner_id, pot, kind="duel_payout", ref_id=f"duel:{duel_id}:payout"
    )

    duel.winner_id = winner_id
    duel.loser_id = loser_id
    duel.fee = fee
    duel.status = "resolved"
    duel.resolved_at = datetime.utcnow()
    await session.commit()

    return {
        "status": "resolved",
        "duel_id": duel_id,
        "winner_id": winner_id,
        "loser_id": loser_id,
        "fee": fee,
        "pot": paid,
        "mute_seconds": duel.mute_seconds,
    }


# --- duelbot (D-08: против банка) --------------------------------------------


async def duelbot(
    session: AsyncSession, chat_id: int, challenger_id: int, stake: int, ref_id: str
) -> dict:
    """`/duelbot` — та же механика coinflip/эскроу/5%-комиссия, что и /duel,
    но соперник — банк чата (opponent_id=BANK_SENTINEL, авто-принятие,
    D-08). На выигрыше челленджера выплата (2*stake-fee) из банка; на
    проигрыше ставка остаётся в банке, челленджер — loser_id."""
    balance = await economy_service.get_balance(session, chat_id, challenger_id)
    _validate_stake(stake, balance)

    escrowed = await _escrow_stake(session, chat_id, challenger_id, stake, ref_id)
    if not escrowed:
        logger.info("duelbot: ref_id=%s уже обработан, пропускаем", ref_id)
        raise DuelAlreadyResolved(f"Запрос на /duelbot уже обработан (ref_id={ref_id})")

    fee, pot = _fee_and_pot(stake)
    challenger_won = bool(_rng.choice([True, False]))

    winner_id = challenger_id if challenger_won else None
    loser_id = None if challenger_won else challenger_id

    paid = 0
    if challenger_won:
        paid = await economy_service.pay_from_bank(
            session, chat_id, challenger_id, pot, kind="duel_payout", ref_id=f"duelbot:{ref_id}:payout"
        )

    duel = Duel(
        chat_id=chat_id,
        challenger_id=challenger_id,
        opponent_id=BANK_SENTINEL,
        stake=stake,
        status="resolved",
        winner_id=winner_id,
        loser_id=loser_id,
        fee=fee,
        mute_seconds=MUTE_SECONDS,
        resolved_at=datetime.utcnow(),
    )
    session.add(duel)
    await session.commit()

    return {
        "status": "resolved",
        "duel_id": duel.id,
        "winner_id": winner_id,
        "loser_id": loser_id,
        "fee": fee,
        "pot": paid,
        "mute_seconds": MUTE_SECONDS,
    }


# --- decline_duel / cancel_duel (полный рефанд, D-08 no-op на статус) -------


async def _refund_pending_duel(
    session: AsyncSession, chat_id: int, duel_id: int, actor_id: int, actor_field: str, new_status: str
) -> dict:
    duel = await _get_duel_for_update(session, chat_id, duel_id)
    if duel.status != "pending":
        await session.commit()
        return {"status": duel.status, "duel_id": duel_id, "refunded": 0}

    if getattr(duel, actor_field) != actor_id:
        await session.commit()
        raise DuelError("Только соответствующий участник дуэли может выполнить это действие")

    refunded = await economy_service.pay_from_bank(
        session,
        chat_id,
        duel.challenger_id,
        duel.stake,
        kind="duel_refund",
        ref_id=f"duel:{duel_id}:refund",
    )
    duel.status = new_status
    await session.commit()
    return {"status": new_status, "duel_id": duel_id, "refunded": refunded}


async def decline_duel(session: AsyncSession, chat_id: int, duel_id: int, actor_id: int) -> dict:
    """Отклоняет pending-дуэль (только приглашённый оппонент) — полный рефанд
    ставки челленджеру, status="declined". Статус-переход — гард
    идемпотентности: повторный вызов на уже нерending-дуэли — no-op."""
    return await _refund_pending_duel(session, chat_id, duel_id, actor_id, "opponent_id", "declined")


async def cancel_duel(session: AsyncSession, chat_id: int, duel_id: int, actor_id: int) -> dict:
    """Отменяет pending-дуэль (только челленджер) — полный рефанд ставки,
    status="cancelled". Статус-переход — гард идемпотентности."""
    return await _refund_pending_duel(session, chat_id, duel_id, actor_id, "challenger_id", "cancelled")
