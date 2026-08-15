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

Джекпот-ивент (запрошено 2026-08-07, пул вырос до 800к+): `/jackpot_event N`
(bot/handlers/owner.py, владелец-онли) ставит `event_spins_remaining = N` —
следующие N спинов слота Azumanga в этом чате гарантированно завершатся
выигрышем НЕ ПОЗЖЕ N-го, вместо обычных `1/slot_jackpot_odds`. Первый же
выигрыш откатывает `event_spins_remaining` обратно в NULL — обычный режим
возобновляется сам собой, отдельной команды "выключить ивент" не нужно."""

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

# Название ивента (запрошено 2026-08-07, владелец) — единственное место,
# задающее строку, чтобы старт/выигрыш-объявления не разошлись в формулировке.
EVENT_NAME = "ГИФТЕКИ ОТ ОСАКИ"


def build_announcement_caption(
    escaped_name: str, amount: int, pool_after: int, *, event: bool = False
) -> str:
    """Текст оповещения о сорванном джекпоте слота — один источник истины на
    формулировку для ДВУХ разных процессов (`api/routes/games.py::
    _announce_jackpot_win` на реальный выигрыш, `bot/handlers/owner.py::
    test_jackpot_command` на ручной тест-триггер владельца), чтобы текст не
    дублировался и не расходился между ними. `escaped_name` — уже
    html.escape()-нутое имя (эта функция не знает о Telegram/HTML, только
    собирает текст).

    `event=True` (contribute_and_maybe_award вернул `event_win=True`) — тот
    же выигрыш, отдельная формулировка, чтобы чат понял, что это разрешило
    именно ивент-гарант `/jackpot_event` («ГИФТЕКИ ОТ ОСАКИ», см.
    EVENT_NAME/build_event_start_caption), а не рядовой 1-к-N бросок.
    `api/routes/games.py::post_slots` шлёт это оповещение ДАЖЕ при
    `amount==0` для ивент-выигрыша (в отличие от обычного, который вообще не
    объявляется при нулевой выплате) — ивент обязан быть публично виден
    независимо от того, капнула ли выплата на опустевшем банке, иначе он
    молча "сгорал" бы без единого слова в чате, что и есть весь смысл
    ивента. Раз при `amount==0` пул НЕ сбрасывается (см. докстринг
    contribute_and_maybe_award), формулировка "обнулён" тут была бы ложью —
    отдельная ветка текста."""
    if event and amount == 0:
        return (
            f"🎰 {EVENT_NAME} — гарант сработал, но банк чата пуст 😬\n"
            f"{escaped_name} поймал(а) гарантированный джекпот, но выплатить прямо сейчас нечего — "
            "банк чата на нуле.\n"
            f"Ивент завершён (пул остался {pool_after}¥, никуда не делся) — обычный режим возобновлён 💰"
        )
    if event:
        return (
            f"🎉🎰 {EVENT_NAME} — ДЖЕКПОТ СОРВАН! 🎰🎉\n"
            f"{escaped_name} поймал(а) гарантированный джекпот — +{amount}¥ прямо из банка чата!\n"
            f"Ивент завершён, пул обнулён до {pool_after}¥ — обычный режим возобновлён 💰"
        )
    return (
        "🎰 ДЖЕКПОТ, ДЖЕКПОТ! 🎰\n"
        f"Джекпот слота достался {escaped_name} — +{amount}¥ прямо из банка чата!\n"
        f"Пул обнулён до {pool_after}¥ — фармим заново 💰"
    )


def build_event_start_caption(pool_now: int, spins: int) -> str:
    """Текст публичного анонса старта джекпот-ивента (владелец шлёт его в чат
    через `/jackpot_event N`, bot/handlers/owner.py) — единственное место,
    собирающее эту формулировку, той же дисциплины, что build_announcement_caption."""
    return (
        f"🔥🎰 {EVENT_NAME}! 🎰🔥\n"
        f"Следующие {spins} круток слота Азуманга в этом чате — гарантированный джекпот, "
        f"кто-то из них точно сорвёт банк (сейчас в пуле {pool_now}¥ и продолжает расти).\n"
        "Успей поймать первым — крути! 🎰"
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


async def start_event(session: AsyncSession, chat_id: int, spins: int) -> int:
    """Запускает джекпот-ивент (владелец, `/jackpot_event N`,
    bot/handlers/owner.py) — ставит `event_spins_remaining = spins` под тем
    же `FOR UPDATE`-локом, что и обычные спины, так что владелец не может
    гонку с уже летящим спином того же чата (либо этот вызов, либо тот спин
    — строго по очереди, как любая другая мутация этой строки).

    `spins` обязан быть положительным (валидация — на вызывающем,
    bot/handlers/owner.py, тот же паттерн, что _parse_args других owner-
    команд) — здесь `assert`-стиль defense in depth, не пользовательский
    путь ошибки. Перезаписывает уже идущий ивент, если такой был (владелец
    сам решает перезапустить — не гоняемся за "нельзя стартовать дважды",
    команда владельческая, доверенная).

    НЕ коммитит — как и весь модуль, транзакцию завершает вызывающий.
    Возвращает текущий пул (для текста анонса)."""
    if spins <= 0:
        raise ValueError(f"spins должен быть положительным, получено {spins}")
    row = await _get_or_seed_pool_locked(session, chat_id)
    row.event_spins_remaining = spins
    return row.pool


async def contribute_and_maybe_award(
    session: AsyncSession,
    chat_id: int,
    user_id: int,
    bet: int,
    idem_key: str,
    rng,
) -> dict:
    """Пополняет пул, затем бросает кубик за джекпот и на удаче выплачивает
    ВЕСЬ пул через pay_from_bank (капается остатком банка, тот же примитив,
    что и обычная выплата слота), сбрасывая пул на seed; на неудаче просто
    возвращает выросший пул.

    Кубик — ОДИН из двух режимов, в зависимости от `row.event_spins_remaining`
    (см. докстринг класса/`start_event`):
      - Обычный режим (NULL/0): независимая монетка 1/`slot_jackpot_odds`,
        как было всегда.
      - Ивент-режим (>0, "гарант в следующих N спинах"): pity-вероятность
        1/remaining на ТЕКУЩИЙ спин, remaining после этого спина всегда
        уменьшается на 1 — при remaining==1 вероятность 1/1 == 100%, так что
        выигрыш гарантированно случается НЕ ПОЗЖЕ N-го спина с начала ивента
        (стандартная "pity"-математика, та же идея, что гача-пити этого же
        бота). Любой выигрыш в ивент-режиме (даже с капнутой на банке
        выплатой) закрывает ивент — `event_spins_remaining` обратно в NULL,
        "первый, кто поймал — забрал и откатываем назад" ровно так, как
        просил владелец, не переживает капнутый по банку payout.

    Если банк чата в момент выигрыша пуст (роздан другими выплатами
    казино/дуэлей) — `pay_from_bank` вернёт 0, и пул НЕ сбрасывается: сброс на
    seed оправдан только тогда, когда пул реально выплачен игроку, иначе
    накопленный много-спиновый пул сгорал бы без единого выплаченного ювика
    ради нулевой выплаты. Ивент при этом всё равно закрывается (см. выше) —
    "гарант" был про сам факт срабатывания кубика, не про размер выплаты.

    Вызывающий (casino_service.play_slots) обязан звать это ТОЛЬКО для
    подтверждённо нового спина (не replay одного и того же idem_key) — иначе
    один и тот же спин пополнял бы пул/бросал кубик/тратил ивент-спин повторно."""
    row = await _get_or_seed_pool_locked(session, chat_id)
    row.pool += int(bet * settings.slot_jackpot_skim_pct)

    event_active = bool(row.event_spins_remaining and row.event_spins_remaining > 0)
    if event_active:
        remaining = row.event_spins_remaining
        won = remaining <= 1 or rng.randint(1, remaining) == 1
        row.event_spins_remaining = remaining - 1
    else:
        won = rng.randint(1, settings.slot_jackpot_odds) == 1

    if not won:
        return {"won": False, "pool": row.pool}

    if event_active:
        row.event_spins_remaining = None

    amount = row.pool
    paid = await economy_service.pay_from_bank(
        session, chat_id, user_id, amount, kind="slot_jackpot", ref_id=f"slot_jackpot:{chat_id}:{idem_key}"
    )
    if paid > 0:
        row.pool = settings.slot_jackpot_seed
    return {"won": True, "amount": paid, "pool": row.pool, "event_win": event_active}
