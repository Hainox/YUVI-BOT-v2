"""Тесты прогрессивного джекпота слота (CASINO-06, bot/services/jackpot_service.py)
против живого Postgres (фикстура `session` — транзакция-на-тест). Доказывают:

- Пул чата создаётся лениво и засеивается `settings.slot_jackpot_seed` при
  первом обращении (`_get_or_seed_pool_locked`/`get_pool`).
- `contribute_and_maybe_award` на каждый вызов пополняет пул на
  `int(bet * settings.slot_jackpot_skim_pct)` независимо от исхода броска.
- На неудаче (rng.randint != 1) пул просто растёт, деньги игроку не платятся.
- На удаче (rng.randint == 1) выплачивается ВЕСЬ накопленный пул через
  `economy_service.pay_from_bank` (капается остатком банка, как обычный
  выигрыш казино, D-06), пул сбрасывается на seed.
- `casino_service.play_slots` зовёт джекпот ТОЛЬКО для подтверждённо нового
  спина — replay того же idem_key возвращает `jackpot=None`, пул не растёт и
  кубик не бросается повторно.

RNG форсируется через простой `_ForcedRng`-стаб (`.randint` фиксированное
значение) — никогда не проверяем реальную случайность.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from bot.config import settings
from bot.services import casino_service
from bot.services import economy_service
from bot.services import jackpot_service
from common.db.session import SessionLocal
from common.models.chat_bank import ChatBank
from common.models.slot_jackpot import SlotJackpot
from common.models.user import User


async def _ensure_user(session, user_id: int, first_name: str = "Тест") -> None:
    session.add(User(id=user_id, first_name=first_name))
    await session.flush()


async def _fund(session, chat_id: int, user_id: int) -> None:
    await economy_service.get_balance(session, chat_id, user_id)


async def _get_bank_balance(session, chat_id: int) -> int:
    result = await session.execute(select(ChatBank.balance).where(ChatBank.chat_id == chat_id))
    return result.scalar_one_or_none() or 0


async def _get_pool_row(session, chat_id: int) -> SlotJackpot | None:
    return (
        await session.execute(select(SlotJackpot).where(SlotJackpot.chat_id == chat_id))
    ).scalar_one_or_none()


class _ForcedRng:
    """Форсирует конкретный исход `randint` — 1 (выигрыш) либо любое другое
    значение (проигрыш)."""

    def __init__(self, randint_value: int):
        self._randint_value = randint_value

    def randint(self, a: int, b: int) -> int:
        return self._randint_value


# --- Ленивое создание/засев пула ---------------------------------------------


@pytest.mark.asyncio
async def test_get_pool_seeds_on_first_read(session):
    chat_id = -100900201
    assert await jackpot_service.get_pool(session, chat_id) == settings.slot_jackpot_seed
    # get_pool сам по себе НЕ создаёт строку (без блокировки, только чтение).
    assert await _get_pool_row(session, chat_id) is None


@pytest.mark.asyncio
async def test_contribute_seeds_pool_row_on_first_spin(session):
    chat_id = -100900202
    user_id = 900202
    await _ensure_user(session, user_id)
    await _fund(session, chat_id, user_id)
    await session.commit()

    bet = 200
    jackpot = await jackpot_service.contribute_and_maybe_award(
        session, chat_id, user_id, bet, "test_jackpot_seed", _ForcedRng(randint_value=2)
    )
    await session.commit()

    expected_pool = settings.slot_jackpot_seed + int(bet * settings.slot_jackpot_skim_pct)
    assert jackpot == {"won": False, "pool": expected_pool}
    row = await _get_pool_row(session, chat_id)
    assert row is not None
    assert row.pool == expected_pool


# --- Пополнение без выигрыша ---------------------------------------------------


@pytest.mark.asyncio
async def test_contribute_accumulates_across_spins(session):
    chat_id = -100900203
    user_id = 900203
    await _ensure_user(session, user_id)
    await _fund(session, chat_id, user_id)
    await session.commit()

    bet = 500
    skim = int(bet * settings.slot_jackpot_skim_pct)
    rng = _ForcedRng(randint_value=42)  # никогда не 1 -> никогда не выигрывает

    first = await jackpot_service.contribute_and_maybe_award(
        session, chat_id, user_id, bet, "test_jackpot_accum_1", rng
    )
    await session.commit()
    second = await jackpot_service.contribute_and_maybe_award(
        session, chat_id, user_id, bet, "test_jackpot_accum_2", rng
    )
    await session.commit()

    assert first == {"won": False, "pool": settings.slot_jackpot_seed + skim}
    assert second == {"won": False, "pool": settings.slot_jackpot_seed + 2 * skim}


# --- Выигрыш и сброс пула -------------------------------------------------------


@pytest.mark.asyncio
async def test_contribute_award_pays_full_pool_and_resets(session):
    chat_id = -100900204
    user_id = 900204
    await _ensure_user(session, user_id)
    await _fund(session, chat_id, user_id)
    await economy_service.credit_bank(
        session, chat_id, 1_000_000, kind="test_seed", ref_id="test_jackpot_award_bank_seed"
    )
    await session.commit()

    bet = 300
    skim = int(bet * settings.slot_jackpot_skim_pct)
    expected_pool_before_award = settings.slot_jackpot_seed + skim

    jackpot = await jackpot_service.contribute_and_maybe_award(
        session, chat_id, user_id, bet, "test_jackpot_win", _ForcedRng(randint_value=1)
    )
    await session.commit()

    assert jackpot == {
        "won": True,
        "amount": expected_pool_before_award,
        "pool": settings.slot_jackpot_seed,
        "event_win": False,
    }
    row = await _get_pool_row(session, chat_id)
    assert row.pool == settings.slot_jackpot_seed


@pytest.mark.asyncio
async def test_contribute_award_capped_to_bank_balance(session):
    chat_id = -100900205
    user_id = 900205
    await _ensure_user(session, user_id)
    await _fund(session, chat_id, user_id)

    small_bank = 7
    await economy_service.credit_bank(
        session, chat_id, small_bank, kind="test_seed", ref_id="test_jackpot_cap_bank_seed"
    )
    await session.commit()

    bet = 500
    jackpot = await jackpot_service.contribute_and_maybe_award(
        session, chat_id, user_id, bet, "test_jackpot_win_capped", _ForcedRng(randint_value=1)
    )
    await session.commit()

    # Пул (seed + skim) заведомо больше крошечного банка -> выплата урезана
    # до остатка банка (D-06, тот же примитив pay_from_bank, что обычный
    # выигрыш казино), но пул всё равно сбрасывается на seed.
    assert jackpot["won"] is True
    assert jackpot["amount"] <= small_bank
    assert jackpot["pool"] == settings.slot_jackpot_seed
    assert await _get_bank_balance(session, chat_id) >= 0


@pytest.mark.asyncio
async def test_contribute_award_with_empty_bank_does_not_reset_pool(session):
    """Баг-регрессия: банк чата пуст (bank_balance == 0, роздан другими
    выплатами казино/дуэлей) в момент срыва джекпота -> `pay_from_bank`
    возвращает paid=0. Накопленный за много спинов пул НЕ должен обнуляться
    до seed ради нулевой выплаты — иначе он сгорал бы без единого
    выплаченного ювика."""
    chat_id = -100900207
    user_id = 900207
    await _ensure_user(session, user_id)
    await _fund(session, chat_id, user_id)
    # ChatBank НЕ пополняется вовсе -> баланс банка остаётся ровно 0.
    await session.commit()
    assert await _get_bank_balance(session, chat_id) == 0

    bet = 500
    skim = int(bet * settings.slot_jackpot_skim_pct)
    losing_rng = _ForcedRng(randint_value=42)  # никогда не 1 -> копим пул без выигрыша

    for i in range(3):
        await jackpot_service.contribute_and_maybe_award(
            session, chat_id, user_id, bet, f"test_jackpot_empty_bank_grow_{i}", losing_rng
        )
        await session.commit()

    pool_before_award = settings.slot_jackpot_seed + 3 * skim
    assert await jackpot_service.get_pool(session, chat_id) == pool_before_award

    jackpot = await jackpot_service.contribute_and_maybe_award(
        session, chat_id, user_id, bet, "test_jackpot_empty_bank_win", _ForcedRng(randint_value=1)
    )
    await session.commit()

    # Ещё один skim добавляется ДО броска (тем же кодом, что и в проигрышных
    # спинах выше) -> пул к моменту ролла на skim больше pool_before_award.
    pool_at_roll = pool_before_award + skim
    assert jackpot["won"] is True
    assert jackpot["amount"] == 0  # банк пуст -> pay_from_bank отдаёт 0
    # Главный инвариант регрессии: пул НЕ сброшен до seed.
    assert jackpot["pool"] == pool_at_roll
    row = await _get_pool_row(session, chat_id)
    assert row.pool == pool_at_roll
    assert await _get_bank_balance(session, chat_id) == 0


@pytest.mark.asyncio
async def test_contribute_award_exception_before_commit_is_rolled_back(monkeypatch):
    """Незафиксированный тестом инвариант (не баг на сегодняшнем коде): если
    `pay_from_bank` падает ПОСЛЕ RNG-выигрыша (до финального
    `session.commit()` в `play_slots`), исключение должно пробрасываться
    наружу необработанным, а откат сессии обязан откатить ЛЮБУЮ несохранённую
    мутацию `row.pool` этого вызова — деньги либо полностью проведены, либо
    не проведены вовсе.

    Намеренно НЕ использует `session`-фикстуру этого файла (она откатывает
    ВСЮ транзакцию теста целиком по завершении — включая уже "закоммиченные"
    внутри теста шаги, т.к. это одна и та же внешняя транзакция; мидтестовый
    `session.rollback()` на ней стирает состояние не только неудачного
    вызова, но и всей предыдущей истории теста). Вместо этого — та же
    дисциплина, что в проде (`api/routes/games.py::post_slots`: отдельная
    `SessionLocal()` на запрос, коммитящая и закрывающаяся ДО следующего):
    предыдущее ("уже подтверждённое") состояние заводится и коммитится в
    ОТДЕЛЬНОЙ, независимо закрытой сессии, затем последующий (падающий)
    вызов — в своей собственной сессии, которую явно откатываем, а проверка
    состояния — уже в ТРЕТЬЕЙ, свежей сессии, как это увидел бы следующий
    реальный HTTP-запрос."""
    chat_id = -100900208
    user_id = 900208
    bet = 500

    async with SessionLocal() as setup_session:
        stmt = (
            pg_insert(User)
            .values(id=user_id, first_name="Тест")
            .on_conflict_do_nothing(index_elements=["id"])
        )
        await setup_session.execute(stmt)
        await setup_session.commit()
        await economy_service.get_balance(setup_session, chat_id, user_id)
        await economy_service.credit_bank(
            setup_session, chat_id, 1_000_000, kind="test_seed", ref_id="test_jackpot_rollback_bank_seed"
        )
        # Растим пул одним проигрышным спином -- реально коммитим (не
        # rollback-изолированная фикстура), чтобы следующий шаг теста
        # столкнулся с ГЕНУИННО подтверждённым "предыдущим" состоянием.
        await jackpot_service.contribute_and_maybe_award(
            setup_session, chat_id, user_id, bet, "test_jackpot_rollback_grow", _ForcedRng(randint_value=42)
        )
        await setup_session.commit()

    async with SessionLocal() as check_session:
        pool_before = await jackpot_service.get_pool(check_session, chat_id)
        bank_before = await _get_bank_balance(check_session, chat_id)
        user_balance_before = await economy_service.get_balance(check_session, chat_id, user_id)

    async def _boom(*args, **kwargs):
        raise RuntimeError("simulated pay_from_bank failure")

    monkeypatch.setattr(economy_service, "pay_from_bank", _boom)

    # Тот же паттерн, что `async with SessionLocal() as session:` в
    # post_slots/play_slots: необработанное исключение поднимается из
    # contribute_and_maybe_award ПОСЛЕ RNG-выигрыша, ДО какого-либо commit --
    # явный rollback (то, что сделал бы __aexit__ на необработанном
    # исключении) обязан откатить несохранённую мутацию row.pool целиком.
    async with SessionLocal() as failing_session:
        with pytest.raises(RuntimeError):
            await jackpot_service.contribute_and_maybe_award(
                failing_session, chat_id, user_id, bet, "test_jackpot_rollback_win", _ForcedRng(randint_value=1)
            )
        await failing_session.rollback()

    async with SessionLocal() as final_session:
        assert await jackpot_service.get_pool(final_session, chat_id) == pool_before
        row = await _get_pool_row(final_session, chat_id)
        assert row.pool == pool_before
        assert await _get_bank_balance(final_session, chat_id) == bank_before
        assert await economy_service.get_balance(final_session, chat_id, user_id) == user_balance_before


# --- Wiring в casino_service.play_slots: только для НОВОГО спина -------------


@pytest.mark.asyncio
async def test_play_slots_replay_does_not_touch_jackpot(session, monkeypatch):
    chat_id = -100900206
    user_id = 900206
    await _ensure_user(session, user_id)
    await _fund(session, chat_id, user_id)
    await session.commit()

    from tests.test_slot_engine import _ForcedGridRng
    from bot.services import slot_engine

    forced_symbols = ["sakaki", "bath-chibi", "osaka-stand", "dog", "gasp"] * 3
    monkeypatch.setattr(casino_service, "_rng", _ForcedGridRng(forced_symbols))

    idem_key = "test_jackpot_replay_guard"
    bet_total = 10 * slot_engine.TOTAL_LINES

    first = await casino_service.play_slots(session, chat_id, user_id, bet_total, idem_key)
    assert first["jackpot"] is not None
    pool_after_first = first["jackpot"]["pool"]

    second = await casino_service.play_slots(session, chat_id, user_id, bet_total, idem_key)
    assert second["jackpot"] is None

    row = await _get_pool_row(session, chat_id)
    assert row.pool == pool_after_first


# --- Джекпот-ивент "ГИФТЕКИ ОТ ОСАКИ" (pity-гарант, /jackpot_event) ---------
#
# start_event ставит event_spins_remaining=N; contribute_and_maybe_award
# бросает 1/remaining вместо обычных 1/slot_jackpot_odds, remaining==1
# ГАРАНТИРУЕТ выигрыш на этом спине (сам код короткого замыкания читай в
# jackpot_service.py — здесь только доказываем поведение снаружи).


@pytest.mark.asyncio
async def test_start_event_sets_remaining_without_touching_pool(session):
    chat_id = -100900210
    user_id = 900210
    await _ensure_user(session, user_id)
    await _fund(session, chat_id, user_id)
    await session.commit()

    # Растим пул одним обычным (не-ивентным) проигрышным спином, чтобы
    # доказать, что start_event ничего не делает с уже накопленным пулом.
    await jackpot_service.contribute_and_maybe_award(
        session, chat_id, user_id, 100, "test_event_pregrowth", _ForcedRng(randint_value=99)
    )
    await session.commit()
    pool_before = await jackpot_service.get_pool(session, chat_id)

    returned_pool = await jackpot_service.start_event(session, chat_id, 100)
    await session.commit()

    assert returned_pool == pool_before
    row = await _get_pool_row(session, chat_id)
    assert row.event_spins_remaining == 100
    assert row.pool == pool_before


@pytest.mark.asyncio
async def test_start_event_rejects_non_positive_spins(session):
    chat_id = -100900211
    with pytest.raises(ValueError):
        await jackpot_service.start_event(session, chat_id, 0)
    with pytest.raises(ValueError):
        await jackpot_service.start_event(session, chat_id, -5)


@pytest.mark.asyncio
async def test_event_forces_win_on_the_very_last_spin_even_with_losing_rng(session):
    """spins=1 -> remaining==1 сразу на первом же спине ивента -> выигрыш
    гарантирован НЕЗАВИСИМО от rng (даже стаб, который в обычном режиме
    ВСЕГДА проигрывает, здесь обязан "выиграть" — это и есть смысл гаранта)."""
    chat_id = -100900212
    user_id = 900212
    await _ensure_user(session, user_id)
    await _fund(session, chat_id, user_id)
    await economy_service.credit_bank(
        session, chat_id, 1_000_000, kind="test_seed", ref_id="test_event_last_spin_bank"
    )
    await session.commit()

    await jackpot_service.start_event(session, chat_id, 1)
    await session.commit()

    never_wins_rng = _ForcedRng(randint_value=999)  # в обычном режиме никогда не 1
    jackpot = await jackpot_service.contribute_and_maybe_award(
        session, chat_id, user_id, 100, "test_event_last_spin", never_wins_rng
    )
    await session.commit()

    assert jackpot["won"] is True
    assert jackpot["event_win"] is True
    row = await _get_pool_row(session, chat_id)
    assert row.event_spins_remaining is None  # ивент закрыт этим выигрышем
    assert row.pool == settings.slot_jackpot_seed


@pytest.mark.asyncio
async def test_event_pity_odds_not_base_odds_while_active(session, monkeypatch):
    """Пока ивент активен, кубик бросается 1/remaining, а НЕ
    1/slot_jackpot_odds — форсируем крошечный slot_jackpot_odds (было бы
    почти гарантированным выигрышем в обычном режиме) и RNG-стаб, который
    "проигрывает" именно тем значениям, что реально спрашивает pity-формула
    (remaining=3,2), доказывая, что используется именно она."""
    monkeypatch.setattr(settings, "slot_jackpot_odds", 2)  # если бы использовался — почти всегда "выигрыш"
    chat_id = -100900213
    user_id = 900213
    await _ensure_user(session, user_id)
    await _fund(session, chat_id, user_id)
    await session.commit()

    await jackpot_service.start_event(session, chat_id, 3)
    await session.commit()

    class _AlwaysHighRng:
        """`.randint(a, b)` всегда возвращает `b` — проигрыш, ПОКА `b > 1`
        (при remaining==1 pity-формула вообще не спрашивает rng, см. её
        короткое замыкание, так что этот стаб не участвует в последнем спине)."""

        def randint(self, a: int, b: int) -> int:
            return b

    rng = _AlwaysHighRng()

    first = await jackpot_service.contribute_and_maybe_award(
        session, chat_id, user_id, 100, "test_event_pity_1", rng
    )
    await session.commit()
    assert first["won"] is False
    assert (await _get_pool_row(session, chat_id)).event_spins_remaining == 2

    second = await jackpot_service.contribute_and_maybe_award(
        session, chat_id, user_id, 100, "test_event_pity_2", rng
    )
    await session.commit()
    assert second["won"] is False
    assert (await _get_pool_row(session, chat_id)).event_spins_remaining == 1

    # Третий (последний) спин: remaining==1 -> гарантированный выигрыш,
    # несмотря на тот же "всегда проигрышный" rng-стаб.
    third = await jackpot_service.contribute_and_maybe_award(
        session, chat_id, user_id, 100, "test_event_pity_3", rng
    )
    await session.commit()
    assert third["won"] is True
    assert third["event_win"] is True


@pytest.mark.asyncio
async def test_event_win_resumes_normal_odds_afterwards(session):
    """"Кто первый успеет поймать — забирает банк и откатываем назад": после
    выигрыша в ивенте следующий спин снова использует обычные
    slot_jackpot_odds, а не pity — событие не "перезапускается" само собой."""
    chat_id = -100900214
    user_id = 900214
    await _ensure_user(session, user_id)
    await _fund(session, chat_id, user_id)
    await economy_service.credit_bank(
        session, chat_id, 1_000_000, kind="test_seed", ref_id="test_event_resume_bank"
    )
    await session.commit()

    await jackpot_service.start_event(session, chat_id, 1)
    await session.commit()
    won_event_spin = await jackpot_service.contribute_and_maybe_award(
        session, chat_id, user_id, 100, "test_event_resume_win", _ForcedRng(randint_value=999)
    )
    await session.commit()
    assert won_event_spin["won"] is True
    assert (await _get_pool_row(session, chat_id)).event_spins_remaining is None

    # Тот же "any non-1 value" rng, что гарантированно выиграл бы в ивенте на
    # remaining==1, теперь снова просто rng.randint(1, slot_jackpot_odds) —
    # никакого специального обращения, обычный проигрыш при большом odds.
    losing_rng = _ForcedRng(randint_value=999)
    after = await jackpot_service.contribute_and_maybe_award(
        session, chat_id, user_id, 100, "test_event_resume_after", losing_rng
    )
    await session.commit()
    assert after["won"] is False


@pytest.mark.asyncio
async def test_event_win_closes_event_even_when_payout_capped_by_empty_bank(session):
    """Тот же D-06-класса регресс, что test_contribute_award_with_empty_bank_
    does_not_reset_pool, но для ивента: банк пуст -> paid=0, пул НЕ
    сбрасывается — но ивент ВСЁ РАВНО закрывается (гарант был про сам факт
    срабатывания кубика, не про размер выплаты, см. докстринг
    contribute_and_maybe_award)."""
    chat_id = -100900215
    user_id = 900215
    await _ensure_user(session, user_id)
    await _fund(session, chat_id, user_id)
    # ChatBank НЕ пополняется -> баланс банка 0.
    await session.commit()

    await jackpot_service.start_event(session, chat_id, 1)
    await session.commit()

    jackpot = await jackpot_service.contribute_and_maybe_award(
        session, chat_id, user_id, 100, "test_event_empty_bank", _ForcedRng(randint_value=999)
    )
    await session.commit()

    assert jackpot["won"] is True
    assert jackpot["event_win"] is True
    assert jackpot["amount"] == 0
    row = await _get_pool_row(session, chat_id)
    assert row.event_spins_remaining is None  # ивент закрыт, несмотря на нулевую выплату
    assert row.pool != settings.slot_jackpot_seed  # пул НЕ сброшен (paid == 0)
