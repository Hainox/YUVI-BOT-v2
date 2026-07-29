"""Прогрессивный джекпот слота (CASINO-06) — один растущий пул на chat_id.

Каждый спин слота (независимо от исхода сетки) пополняет пул на
`settings.slot_jackpot_skim_pct` от ставки и бросает отдельную, полностью
независимую монетку — 1 к `settings.slot_jackpot_odds`. На удаче victim
получает ВЕСЬ накопленный пул, пул сбрасывается на `settings.slot_jackpot_seed`.

Деньги двигает ТОЛЬКО через `economy_service.pay_from_bank` (та же
дисциплина, что `casino_service._settle`/`victim_service.run_victim`) — этот
модуль сам никогда не пишет `user_balance`/`chat_bank` напрямую. Не
коммитит — транзакцию завершает вызывающий (`casino_service.play_slots`).

Пул читается/меняется под `FOR UPDATE` на всю операцию (`_get_or_seed_pool_locked`),
чтобы два конкурентных спина одного чата не читали один и тот же "старый"
пул и оба не выросли независимо, теряя прирост одного из них.
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import settings
from bot.services import economy_service
from common.models.slot_jackpot import SlotJackpot

# Репозиторный ассет (не генерится/не грузится динамически) — и api/, и bot/
# образы копируют ВЕСЬ репозиторий (`COPY . /app`), поэтому файл доступен
# локально с диска в обоих процессах: api/routes/games.py (реальные выигрыши)
# и bot/handlers/owner.py (/test_jackpot — ручная визуальная проверка).
JACKPOT_GIF_PATH = Path(__file__).resolve().parent.parent.parent / "miniapp" / "static" / "casino" / "jackpot.gif"


def build_announcement_caption(escaped_name: str, amount: int, pool_after: int) -> str:
    """Текст оповещения о сорванном джекпоте слота — один источник истины на
    формулировку для ДВУХ разных процессов (`api/routes/games.py::
    _announce_jackpot_win` на реальный выигрыш, `bot/handlers/owner.py::
    test_jackpot_command` на ручной тест-триггер владельца), чтобы текст не
    дублировался и не расходился между ними. `escaped_name` — уже
    html.escape()-нутое имя (эта функция не знает о Telegram/HTML, только
    собирает текст)."""
    return (
        "🎰 ДЖЕКПОТ, ДЖЕКПОТ! 🎰\n"
        f"Джекпот слота достался {escaped_name} — +{amount}¥ прямо из банка чата!\n"
        f"Пул обнулён до {pool_after}¥ — фармим заново 💰"
    )


async def _get_or_seed_pool_locked(session: AsyncSession, chat_id: int) -> SlotJackpot:
    """FOR UPDATE-строка пула чата — создаёт с seed-значением, если это
    первый спин слота в этом чате вообще (idempotent upsert, ON CONFLICT DO
    NOTHING, затем SELECT ... FOR UPDATE — та же форма, что
    economy_service._get_or_create_balance для user_balance)."""
    insert_stmt = pg_insert(SlotJackpot).values(chat_id=chat_id, pool=settings.slot_jackpot_seed)
    insert_stmt = insert_stmt.on_conflict_do_nothing(index_elements=["chat_id"])
    await session.execute(insert_stmt)

    return (
        await session.execute(
            select(SlotJackpot).where(SlotJackpot.chat_id == chat_id).with_for_update()
        )
    ).scalar_one()


async def get_pool(session: AsyncSession, chat_id: int) -> int:
    """Текущий размер пула БЕЗ блокировки строки — для показа в UI/статусе,
    не для мутации (см. _get_or_seed_pool_locked)."""
    row = (
        await session.execute(select(SlotJackpot).where(SlotJackpot.chat_id == chat_id))
    ).scalar_one_or_none()
    return row.pool if row is not None else settings.slot_jackpot_seed


async def contribute_and_maybe_award(
    session: AsyncSession,
    chat_id: int,
    user_id: int,
    bet: int,
    idem_key: str,
    rng,
) -> dict:
    """Пополняет пул, затем независимо от сетки бросает 1/slot_jackpot_odds —
    на удаче выплачивает ВЕСЬ пул через pay_from_bank (капается остатком
    банка, тот же примитив, что и обычная выплата слота) и сбрасывает пул на
    seed; на неудаче просто возвращает выросший пул.

    Если банк чата в момент выигрыша пуст (роздан другими выплатами
    казино/дуэлей) — `pay_from_bank` вернёт 0, и пул НЕ сбрасывается: сброс на
    seed оправдан только тогда, когда пул реально выплачен игроку, иначе
    накопленный много-спиновый пул сгорал бы без единого выплаченного ювика
    ради нулевой выплаты.

    Вызывающий (casino_service.play_slots) обязан звать это ТОЛЬКО для
    подтверждённо нового спина (не replay одного и того же idem_key) — иначе
    один и тот же спин пополнял бы пул/бросал кубик повторно."""
    row = await _get_or_seed_pool_locked(session, chat_id)
    row.pool += int(bet * settings.slot_jackpot_skim_pct)

    won = rng.randint(1, settings.slot_jackpot_odds) == 1
    if not won:
        return {"won": False, "pool": row.pool}

    amount = row.pool
    paid = await economy_service.pay_from_bank(
        session, chat_id, user_id, amount, kind="slot_jackpot", ref_id=f"slot_jackpot:{chat_id}:{idem_key}"
    )
    if paid > 0:
        row.pool = settings.slot_jackpot_seed
    return {"won": True, "amount": paid, "pool": row.pool}
