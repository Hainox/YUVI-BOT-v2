"""Разовый случайный розыгрыш ювиков (`/giveaway`, bot/handlers/owner.py) —
лёгкая, on-demand версия «Yuvi_Yuvi дня»: владелец может запускать сколько
угодно раз в день, каждый вызов — НЕЗАВИСИМЫЙ свежий ролл среди участников
экономики чата, приз минтится (economy_service.credit), а не берётся из
банка чата (запрошено 2026-07-28 вместе с планом версии 2.0).

Сознательно НЕ использует daily_pick_service.get_or_set_pick — тот
идемпотентен по (chat_id, kind, day_msk) и на второй вызов в тот же день
вернул бы ТОГО ЖЕ победителя без нового ролла (нужно для «Yuvi_Yuvi дня»,
но ломает цель этой команды: несколько раздельных розыгрышей в один день).
Идемпотентность здесь — только по ref_id сообщения (форма credit/grant),
как страховка от повторной обработки одного и того же апдейта Telegram, а
не как ограничение «раз в день».

Кандидаты — участники экономики чата (та же форма, что
economy_service.credit_all_in_chat/get_leaderboard): у кого уже есть строка
UserBalance в этом чате, не буквально каждый участник Telegram-чата.
Владелец бота участвует наравне со всеми (без исключения).

RNG — собственный модульный seam `_rng` (форма lottery_service/
daily_pick_service/duel_service и т.д.), не импортируется из другого модуля.
"""

from __future__ import annotations

import secrets

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.services import economy_service
from common.models.economy_tx import EconomyTx
from common.models.user_balance import UserBalance

_rng = secrets.SystemRandom()

GIVEAWAY_KIND = "owner_giveaway"


async def _economy_candidates(session: AsyncSession, chat_id: int) -> list[int]:
    """Кандидаты в розыгрыш — участники экономики чата (та же форма, что
    credit_all_in_chat/get_leaderboard)."""
    result = await session.execute(
        select(UserBalance.user_id).where(UserBalance.chat_id == chat_id)
    )
    return result.scalars().all()


async def run_giveaway(session: AsyncSession, chat_id: int, amount: int, ref_id: str) -> dict:
    """Выбирает СВЕЖЕГО случайного победителя среди участников экономики
    чата и минтит ему `amount` ювиков (economy_service.credit — как
    /grant/grant_all, НЕ pay_from_bank: приз не капается банком чата).

    Идемпотентность — по ref_id (повтор одного и того же message_id не
    должен ни перевыбирать победителя, ни начислять повторно): ролл
    происходит ДО credit(), поэтому при replay (credit() вернёт
    credited=False) функция дополнительно перечитывает РЕАЛЬНОГО уже
    оплаченного победителя И реально начисленную ему сумму из economy_tx
    по (chat_id, kind, ref_id) — иначе повторный вызов показал бы новый
    случайный ролл и/или сумму текущего (повторного) вызова вместо уже
    выплаченного результата (деньги при этом и так не задвоятся:
    UNIQUE(chat_id, ref_id, kind) на economy_tx не зависит от user_id).

    Коммитит. Возвращает {winner, amount, credited, total_candidates} —
    при credited=True amount равен переданному аргументу, при
    credited=False (replay) — исторически начисленной сумме из economy_tx,
    даже если текущий вызов передал другой amount.
    Если в чате ещё нет ни одного участника экономики — {winner: None,
    amount, credited: False, total_candidates: 0} (проверка ДО _rng.choice —
    вызов на пустом списке кандидатов поднимает ValueError)."""
    candidates = await _economy_candidates(session, chat_id)
    total_candidates = len(candidates)
    if total_candidates == 0:
        return {"winner": None, "amount": amount, "credited": False, "total_candidates": 0}

    winner = _rng.choice(candidates)
    try:
        credited = await economy_service.credit(
            session, chat_id, winner, amount, kind=GIVEAWAY_KIND, ref_id=ref_id
        )
        if not credited:
            # Replay одного и того же message_id — возвращаем РЕАЛЬНОГО ранее
            # оплаченного победителя И реально начисленную ему сумму (а не
            # наш только что случайно выбранный winner / переданный текущим
            # вызовом amount — они могут не совпасть с историческими).
            winner, amount = (
                await session.execute(
                    select(EconomyTx.user_id, EconomyTx.amount).where(
                        EconomyTx.chat_id == chat_id,
                        EconomyTx.kind == GIVEAWAY_KIND,
                        EconomyTx.ref_id == ref_id,
                    )
                )
            ).one()

        await session.commit()
    except Exception:
        # Явный rollback вместо неявной подчистки в DbSessionMiddleware —
        # тот же прецедент, что WR-05 (05-REVIEW.md, см.
        # test_victim_service.py::test_victim_handler_rolls_back_session_on_grant_title_db_error):
        # любая НЕ-IntegrityError ошибка после того, как RNG уже выбрал
        # победителя (credit()/commit()/повторное чтение amount выше), не
        # должна оставлять сессию в aborted-состоянии или молча удерживать
        # частично применённые изменения баланса.
        await session.rollback()
        raise
    return {
        "winner": winner,
        "amount": amount,
        "credited": credited,
        "total_candidates": total_candidates,
    }
