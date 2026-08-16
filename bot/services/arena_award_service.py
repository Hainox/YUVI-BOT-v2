"""Атомарная выдача Arena-наград.

Номинация и выплата разделены намеренно: номинация фиксирует победителя, а
этот модуль двигает деньги только в одной транзакции. Порядок блокировок
совпадает с экономическим ядром: award -> user_balance -> chat_bank ->
ArenaFund. ArenaFund расходуется первым, затем остаток берётся из общего
банка чата. Частичная выплата сохраняется в ``paid_amount`` и безопасно
продолжается следующим запуском без повторного начисления.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from bot.services import economy_service
from common.models.arena import ArenaDailyAward
from common.models.arena import ArenaFund
from common.models.arena import ArenaFundLedger
from common.models.arena import ArenaWeeklyAward
from common.models.chat_bank import ChatBank


class _Award(Protocol):
    id: int
    chat_id: int
    user_id: int
    reward_amount: int
    paid_amount: int
    settlement_status: str
    payment_source: str | None
    fund_ledger_key: str
    details: dict | None


@dataclass(frozen=True)
class AwardSettlement:
    award_id: int
    requested: int
    paid: int
    status: str
    payment_source: str | None


async def _lock_chat_bank(session: AsyncSession, chat_id: int) -> None:
    """Создаёт нулевой банк при необходимости и удерживает его lock.

    Нулевой upsert нужен, чтобы транзакции, которым банк пока не пополнялся,
    всё равно соблюдали единый порядок блокировок.
    """
    await session.execute(
        pg_insert(ChatBank)
        .values(chat_id=chat_id, balance=0)
        .on_conflict_do_nothing(index_elements=["chat_id"])
    )
    await session.execute(
        select(ChatBank.chat_id)
        .where(ChatBank.chat_id == chat_id)
        .with_for_update()
    )


async def _debit_arena_fund(
    session: AsyncSession,
    chat_id: int,
    amount: int,
    *,
    idempotency_key: str,
) -> int:
    """Списывает до ``amount`` из ArenaFund и пишет отрицательный ledger row."""
    if amount <= 0:
        return 0

    await session.execute(
        pg_insert(ArenaFund)
        .values(chat_id=chat_id, balance=0)
        .on_conflict_do_nothing(index_elements=["chat_id"])
    )
    fund = (
        await session.execute(
            select(ArenaFund).where(ArenaFund.chat_id == chat_id).with_for_update()
        )
    ).scalar_one()
    paid = min(amount, max(0, fund.balance))
    if paid <= 0:
        return 0

    fund.balance -= paid
    await session.flush()
    session.add(
        ArenaFundLedger(
            chat_id=chat_id,
            amount=-paid,
            balance_after=fund.balance,
            kind="arena_award",
            idempotency_key=idempotency_key,
            note="Arena award payout",
        )
    )
    return paid


def _merge_source(previous: str | None, current: str | None) -> str | None:
    if not current:
        return previous
    if not previous or previous == current:
        return current
    return "mixed"


async def settle_award(session: AsyncSession, award: _Award) -> AwardSettlement:
    """Выплатить одну уже заблокированную nomination row.

    Вызывающий обязан выбрать award через ``FOR UPDATE``. Все изменения
    (источник денег, user_balance, EconomyTx и paid_amount) остаются в
    текущей транзакции и не коммитятся здесь.
    """
    requested = max(0, int(award.reward_amount))
    already_paid = min(requested, max(0, int(award.paid_amount)))
    # ``paid_amount`` is the financial source of truth. A stale status must
    # never hide an underpaid row after a manual repair or old deployment.
    if already_paid >= requested:
        if award.settlement_status != "paid":
            award.settlement_status = "paid"
        return AwardSettlement(
            award.id, requested, already_paid, "paid", award.payment_source
        )

    remaining = requested - already_paid
    # Lock order: user -> chat bank -> ArenaFund. This matches transfer and
    # Arena match settlement, preventing cross-feature deadlocks.
    await economy_service.peek_balance(session, award.chat_id, award.user_id)
    await _lock_chat_bank(session, award.chat_id)

    fund_paid = await _debit_arena_fund(
        session,
        award.chat_id,
        remaining,
        idempotency_key=f"{award.fund_ledger_key}:{already_paid}:fund",
    )
    bank_paid = await economy_service.pay_from_bank(
        session,
        award.chat_id,
        award.user_id,
        remaining - fund_paid,
        kind="arena_award",
        ref_id=f"{award.fund_ledger_key}:{already_paid}:bank",
    )

    paid_now = fund_paid + bank_paid
    if fund_paid:
        credited = await economy_service.credit(
            session,
            award.chat_id,
            award.user_id,
            fund_paid,
            kind="arena_award",
            ref_id=f"{award.fund_ledger_key}:{already_paid}:fund:user",
        )
        if not credited:
            # Source debit and user credit must never diverge. An old ledger
            # with no matching user leg is an integrity failure, so rollback.
            raise RuntimeError(f"Arena award {award.id}: duplicate fund credit ref")

    if paid_now <= 0:
        award.settlement_status = "pending"
        return AwardSettlement(award.id, requested, already_paid, "pending", award.payment_source)

    total_paid = already_paid + paid_now
    current_source = (
        "arena_fund" if fund_paid and not bank_paid else
        "chat_bank" if bank_paid and not fund_paid else
        "mixed"
    )
    award.paid_amount = total_paid
    award.payment_source = _merge_source(award.payment_source, current_source)
    award.settlement_status = "paid" if total_paid >= requested else "partial"
    details = dict(award.details or {})
    details["settlement_status"] = award.settlement_status
    details["paid_amount"] = total_paid
    details["payment_source"] = award.payment_source
    award.details = details
    return AwardSettlement(
        award.id,
        requested,
        total_paid,
        award.settlement_status,
        award.payment_source,
    )


async def _settle_rows(session: AsyncSession, model, where_clause) -> list[AwardSettlement]:
    rows = list(
        (
            await session.execute(
                select(model)
                .where(where_clause, model.paid_amount < model.reward_amount)
                .order_by(model.id.asc())
                .with_for_update(skip_locked=True)
            )
        ).scalars().all()
    )
    results: list[AwardSettlement] = []
    for row in rows:
        results.append(await settle_award(session, row))
    return results


async def settle_daily_awards(
    session: AsyncSession, chat_id: int, award_date
) -> list[AwardSettlement]:
    """Выплатить все незакрытые daily nominations за дату."""
    return await _settle_rows(
        session,
        ArenaDailyAward,
        (ArenaDailyAward.chat_id == chat_id) & (ArenaDailyAward.award_date == award_date),
    )


async def settle_weekly_awards(
    session: AsyncSession, chat_id: int, week_start
) -> list[AwardSettlement]:
    """Выплатить все незакрытые weekly nominations за неделю."""
    return await _settle_rows(
        session,
        ArenaWeeklyAward,
        (ArenaWeeklyAward.chat_id == chat_id) & (ArenaWeeklyAward.week_start == week_start),
    )


async def settle_pending_awards(
    session: AsyncSession, chat_id: int
) -> list[AwardSettlement]:
    """Повторить pending/partial выдачи прошлых дней и недель.

    Это не даёт временно пустому фонду превратить законную награду в
    потерянную: следующий тик продолжает ровно с ``paid_amount``.
    """
    results = await _settle_rows(
        session,
        ArenaDailyAward,
        ArenaDailyAward.chat_id == chat_id,
    )
    results.extend(
        await _settle_rows(
            session,
            ArenaWeeklyAward,
            ArenaWeeklyAward.chat_id == chat_id,
        )
    )
    return results


def register_pending_award_settlement(scheduler, *, minutes: int = 15) -> None:
    """Зарегистрировать retry job для частичных/ожидающих выплат."""
    from bot.config import settings
    from common.db.session import SessionLocal

    async def _job() -> None:
        async with SessionLocal() as session:
            try:
                await settle_pending_awards(session, settings.chat_id)
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    scheduler.add_job(
        _job,
        "interval",
        minutes=minutes,
        coalesce=True,
        max_instances=1,
        misfire_grace_time=3600,
        id="arena_award_settlement",
        replace_existing=True,
    )
