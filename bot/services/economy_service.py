"""Экономическое ядро «Ювики» (ECON-01/02/03) — ЕДИНСТВЕННЫЙ модуль в проекте,
которому разрешено писать user_balance/chat_bank/economy_tx.

Денежные инварианты (RESEARCH.md «Money-Integrity Patterns»):
- Все деньги — только `int` (никаких float/Decimal для сумм); `settings.transfer_fee_pct`
  участвует лишь в промежуточном умножении перед `math.ceil(...)`, результат всегда int.
- Списание — только атомарный guarded `UPDATE ... WHERE balance >= :amount` (Pattern 3):
  отрицательный баланс структурно невозможен, никакого TOCTOU-чтения без блокировки.
- Идемпотентность разовых операций — `ref_id` + частичный UNIQUE(chat_id, ref_id, kind) +
  SAVEPOINT (`session.begin_nested()`), см. RESEARCH.md Pattern 2. Повтор одного и того же
  ref_id ловит IntegrityError внутри SAVEPOINT — откатывается только вложенный блок,
  внешняя транзакция остаётся жива.
- Глобальный контракт порядка блокировок (RESEARCH.md Pattern 1): markets -> market_options
  -> user_balance (по возрастанию user_id) -> chat_bank (всегда последним).
- economy_tx — append-only: здесь НИКОГДА не делается UPDATE или DELETE строк EconomyTx,
  только INSERT (см. _log_tx ниже).
"""

from __future__ import annotations

import logging
import math

from sqlalchemy import func
from sqlalchemy import insert
from sqlalchemy import select
from sqlalchemy import update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import settings
from common.models.chat_bank import ChatBank
from common.models.economy_tx import EconomyTx
from common.models.market import Market
from common.models.user import User
from common.models.user_balance import UserBalance

logger = logging.getLogger(__name__)


class EconomyError(Exception):
    """Базовое исключение экономического модуля."""


class InvalidArgument(EconomyError):
    """Невалидные входные данные операции (сумма/цель)."""


class InsufficientFunds(EconomyError):
    """Недостаточно ювиков для списания."""


# --- Внутренние атомарные примитивы (RESEARCH.md Pattern 3) ----------------


async def _get_or_create_balance(session: AsyncSession, chat_id: int, user_id: int) -> int:
    """Идемпотентный get-or-create кошелька с начислением стартового бонуса
    (ECON-01) при первом обращении. `on_conflict_do_nothing` — единственный
    INSERT, который может выиграть гонку за (chat_id, user_id), поэтому
    start_bonus-транзакция логируется ровно один раз."""
    stmt = (
        pg_insert(UserBalance)
        .values(chat_id=chat_id, user_id=user_id, balance=settings.economy_start_bonus)
        .on_conflict_do_nothing(index_elements=["chat_id", "user_id"])
        .returning(UserBalance.balance)
    )
    row = (await session.execute(stmt)).first()
    if row is not None:
        await _log_tx(
            session,
            chat_id,
            user_id,
            settings.economy_start_bonus,
            "start_bonus",
            ref_id=f"start_bonus:{chat_id}:{user_id}",
        )
        return row.balance

    existing = (
        await session.execute(
            select(UserBalance.balance)
            .where(UserBalance.chat_id == chat_id, UserBalance.user_id == user_id)
            .with_for_update()
        )
    ).scalar_one()
    return existing


async def _guarded_debit(session: AsyncSession, chat_id: int, user_id: int, amount: int) -> bool:
    """Атомарное guarded-списание — один round-trip, отдельная блокировка не
    нужна: блокировка строки самим UPDATE и есть контроль конкурентности.
    rowcount == 0 означает недостаточно средств — отрицательный баланс
    структурно невозможен."""
    stmt = (
        update(UserBalance)
        .where(
            UserBalance.chat_id == chat_id,
            UserBalance.user_id == user_id,
            UserBalance.balance >= amount,
        )
        .values(balance=UserBalance.balance - amount)
    )
    result = await session.execute(stmt)
    return result.rowcount == 1


async def _credit(session: AsyncSession, chat_id: int, user_id: int, amount: int) -> None:
    """Атомарное начисление (инкремент баланса)."""
    stmt = (
        update(UserBalance)
        .where(UserBalance.chat_id == chat_id, UserBalance.user_id == user_id)
        .values(balance=UserBalance.balance + amount)
    )
    await session.execute(stmt)


async def _credit_bank(session: AsyncSession, chat_id: int, amount: int) -> None:
    """Get-or-create банка чата + атомарный инкремент одним upsert-statement'ом."""
    stmt = pg_insert(ChatBank).values(chat_id=chat_id, balance=amount)
    stmt = stmt.on_conflict_do_update(
        index_elements=["chat_id"],
        set_={"balance": ChatBank.balance + amount},
    )
    await session.execute(stmt)


async def _log_tx(
    session: AsyncSession,
    chat_id: int,
    user_id: int | None,
    amount: int,
    kind: str,
    ref_id: str | None,
    note: str | None = None,
) -> None:
    """Единственный способ писать в economy_tx — только INSERT (append-only,
    ECON-03). Если ref_id уже занят для (chat_id, ref_id, kind), партиал-UNIQUE
    индекс поднимет IntegrityError — обрабатывается вызывающим через SAVEPOINT."""
    stmt = insert(EconomyTx).values(
        chat_id=chat_id, user_id=user_id, amount=amount, kind=kind, ref_id=ref_id, note=note
    )
    await session.execute(stmt)


# --- Публичный API (сигнатуры зафиксированы — переиспользуются планами 03-04..06) --


async def get_balance(session: AsyncSession, chat_id: int, user_id: int) -> int:
    """Баланс участника; при первом обращении начисляет стартовый бонус
    (ECON-01) и коммитит его — самодостаточная read-операция, баланс должен
    пережить закрытие текущей сессии."""
    balance = await _get_or_create_balance(session, chat_id, user_id)
    await session.commit()
    return balance


async def transfer_with_fee(
    session: AsyncSession,
    chat_id: int,
    from_user_id: int,
    to_user_id: int,
    amount: int,
    ref_id: str,
) -> None:
    """Перевод с комиссией в банк чата (D-04): fee = max(1, ceil(amount * transfer_fee_pct)).

    Контракт порядка блокировок (Pattern 1): user_balance по возрастанию
    user_id, банк — последним. Идемпотентность (Pattern 2): повтор с тем же
    ref_id ловит IntegrityError на _log_tx внутри SAVEPOINT — no-op.
    """
    if amount <= 0:
        raise InvalidArgument("Сумма перевода должна быть положительной")
    if from_user_id == to_user_id:
        raise InvalidArgument("Нельзя перевести самому себе")

    fee = max(1, math.ceil(amount * settings.transfer_fee_pct))

    first_id, second_id = sorted((from_user_id, to_user_id))
    await _get_or_create_balance(session, chat_id, first_id)
    await _get_or_create_balance(session, chat_id, second_id)

    try:
        async with session.begin_nested():
            debited = await _guarded_debit(session, chat_id, from_user_id, amount)
            if not debited:
                raise InsufficientFunds(f"Недостаточно ювиков (нужно {amount})")
            await _credit(session, chat_id, to_user_id, amount - fee)
            await _log_tx(session, chat_id, from_user_id, -amount, "transfer_out", ref_id)
            await _log_tx(session, chat_id, to_user_id, amount - fee, "transfer_in", ref_id)

            # Контракт порядка блокировок, шаг 4: банк — последним.
            await _credit_bank(session, chat_id, fee)
            await _log_tx(session, chat_id, None, fee, "transfer_fee", ref_id)
    except IntegrityError:
        # Тот же ref_id уже обработан (ретрай апдейта Telegram) — идемпотентный no-op.
        logger.info("transfer_with_fee: ref_id=%s уже обработан, пропускаем", ref_id)
        return

    await session.commit()


async def credit(
    session: AsyncSession, chat_id: int, user_id: int, amount: int, kind: str, ref_id: str
) -> bool:
    """Идемпотентное разовое начисление (payout/refund рынков и т.п.).
    Возвращает False, если ref_id уже применялся — деньги НЕ начисляются
    повторно. Не коммитит — транзакцию завершает вызывающий."""
    await _get_or_create_balance(session, chat_id, user_id)
    try:
        async with session.begin_nested():
            await _credit(session, chat_id, user_id, amount)
            await _log_tx(session, chat_id, user_id, amount, kind, ref_id)
    except IntegrityError:
        logger.info("credit: ref_id=%s (kind=%s) уже применён, пропускаем", ref_id, kind)
        return False
    return True


async def debit(
    session: AsyncSession, chat_id: int, user_id: int, amount: int, kind: str, ref_id: str
) -> bool:
    """Идемпотентное разовое списание (ставка, комиссия создания рынка и т.п.).
    _guarded_debit и _log_tx — строго в ОДНОМ SAVEPOINT: иначе повтор ref_id
    списал бы деньги повторно, залогировав только первый раз. Возвращает
    False, если ref_id уже применялся. Поднимает InsufficientFunds, если
    средств не хватает. Не коммитит — транзакцию завершает вызывающий."""
    await _get_or_create_balance(session, chat_id, user_id)
    try:
        async with session.begin_nested():
            debited = await _guarded_debit(session, chat_id, user_id, amount)
            if not debited:
                raise InsufficientFunds(f"Недостаточно ювиков (нужно {amount})")
            await _log_tx(session, chat_id, user_id, -amount, kind, ref_id)
    except IntegrityError:
        logger.info("debit: ref_id=%s (kind=%s) уже применён, пропускаем", ref_id, kind)
        return False
    return True


async def credit_bank(
    session: AsyncSession, chat_id: int, amount: int, kind: str, ref_id: str
) -> bool:
    """Идемпотентное пополнение банка чата (комиссии рынков и т.п.). Не
    коммитит — транзакцию завершает вызывающий."""
    try:
        async with session.begin_nested():
            await _credit_bank(session, chat_id, amount)
            await _log_tx(session, chat_id, None, amount, kind, ref_id)
    except IntegrityError:
        logger.info("credit_bank: ref_id=%s (kind=%s) уже применён, пропускаем", ref_id, kind)
        return False
    return True


async def get_leaderboard(session: AsyncSession, chat_id: int, limit: int = 10) -> list[dict]:
    """Топ участников чата по балансу (для /leaderboard)."""
    stmt = (
        select(UserBalance.user_id, User.first_name, User.username, UserBalance.balance)
        .join(User, User.id == UserBalance.user_id)
        .where(UserBalance.chat_id == chat_id)
        .order_by(UserBalance.balance.desc())
        .limit(limit)
    )
    result = await session.execute(stmt)
    return [
        {
            "user_id": row.user_id,
            "first_name": row.first_name,
            "username": row.username,
            "balance": row.balance,
        }
        for row in result.all()
    ]


async def get_chat_summary(session: AsyncSession, chat_id: int) -> dict:
    """Сводка по экономике чата (D-06, для /economy): банк, сумма в обороте,
    число открытых рынков. НЕ дублирует /leaderboard и /rules."""
    bank_balance = (
        await session.execute(select(ChatBank.balance).where(ChatBank.chat_id == chat_id))
    ).scalar_one_or_none() or 0

    total_in_circulation = (
        await session.execute(
            select(func.coalesce(func.sum(UserBalance.balance), 0)).where(
                UserBalance.chat_id == chat_id
            )
        )
    ).scalar_one()

    open_markets_count = (
        await session.execute(
            select(func.count())
            .select_from(Market)
            .where(Market.chat_id == chat_id, Market.status == "open")
        )
    ).scalar_one()

    return {
        "bank_balance": int(bank_balance),
        "total_in_circulation": int(total_in_circulation),
        "open_markets_count": int(open_markets_count),
    }
