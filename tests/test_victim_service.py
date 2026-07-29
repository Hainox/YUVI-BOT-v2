"""Интеграционные тесты «Жертвы дня» (VICTIM-01/02, D-05/D-06/D-09/D-10)
против живого Postgres (фикстура `session`) + юнит-тест хендлера
bot/handlers/victim.py (AsyncMock `bot` из tests/conftest.py, форма
test_duel_service.py). Доказывают:

- Идемпотентность пика по MSK-дню (D-09): повторный run_victim в тот же день
  возвращает ТУ ЖЕ жертву (is_new=False), банк списывается на приз ровно
  один раз (Pitfall 5).
- Приз 228 ювиков (D-05) идёт из банка чата через economy_service.pay_from_bank
  (idempotent ref_id, bank-cap — приз урезается до остатка банка, никогда не
  уводит банк в минус).
- Дебафф — удвоенная комиссия перевода (D-06): transfer_with_fee(fee_multiplier=2.0)
  списывает в банк ровно вдвое больше комиссии, чем default fee_multiplier=1.0.
- is_active_victim — окно дебаффа привязано к expires_at/дню: активная жертва
  сегодня, но НЕ вчерашняя (дебафф прошлого дня уже снят).
- Новый MSK-день = новый пик (self-reset по UNIQUE(chat_id, kind, day_msk),
  VICTIM-02/Pitfall 4) — не требует явного DELETE-сброса.
- Хендлер /victim выдаёт реальный Telegram custom_title через
  tag_service.grant_title(source='victim') после коммита приза (D-10).
"""

from __future__ import annotations

from datetime import date
from datetime import datetime
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

import bot.handlers.victim as victim_handlers
from bot.services import daily_pick_service
from bot.services import economy_service
from bot.services import victim_service
from common.models.chat_bank import ChatBank
from common.models.daily_pick import DailyPick
from common.models.daily_stat import DailyStat
from common.models.user import User
from common.models.user_balance import UserBalance


# --- Хелперы (форма test_duel_service.py) ------------------------------------


async def _ensure_user(session, user_id: int, first_name: str = "Тест") -> None:
    session.add(User(id=user_id, first_name=first_name))
    await session.flush()


async def _fund(session, chat_id: int, user_id: int) -> int:
    """Заводит кошелёк (стартовый бонус) и коммитит."""
    return await economy_service.get_balance(session, chat_id, user_id)


async def _seed_daily_stat(session, chat_id: int, user_id: int) -> None:
    """Делает участника кандидатом в жертвы дня — victim_service._active_candidates
    читает distinct daily_stats.user_id по chat_id."""
    session.add(DailyStat(chat_id=chat_id, user_id=user_id, stat_date=date.today(), message_count=1))
    await session.flush()


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


class _ForcedChoiceRng:
    """Тестовый RNG-стаб, monkeypatched вместо `daily_pick_service._rng`.
    Форсирует детерминированный результат `.choice(seq)`."""

    def __init__(self, choice_value):
        self._choice_value = choice_value

    def choice(self, seq):
        return self._choice_value


class _FixedUtcNow:
    """Тестовый стаб, monkeypatched вместо `victim_service.datetime` —
    форсирует детерминированный `.utcnow()` (реальный `timedelta` не
    трогаем, он импортирован в victim_service отдельно)."""

    current: datetime

    def __init__(self, current: datetime):
        self.current = current

    def utcnow(self):
        return self.current


def _fake_message(
    chat_id: int,
    user_id: int,
    first_name: str,
    text: str,
    *,
    message_id: int = 1,
):
    """Минимальный aiogram-подобный Message (форма test_economy_handlers.py)."""
    return SimpleNamespace(
        chat=SimpleNamespace(id=chat_id),
        from_user=SimpleNamespace(id=user_id, first_name=first_name),
        message_id=message_id,
        text=text,
        reply_to_message=None,
        entities=None,
        answer=AsyncMock(),
        reply=AsyncMock(),
    )


# --- Идемпотентность пика + приз из банка (D-09/D-05, Pitfall 5) ------------


@pytest.mark.asyncio
async def test_victim_idempotent_same_day(session, monkeypatch):
    chat_id = -1009004001
    uid = 9004001
    await _ensure_user(session, uid, "Жертва")
    await _fund(session, chat_id, uid)
    await _seed_daily_stat(session, chat_id, uid)
    await economy_service.credit_bank(
        session, chat_id, 10_000, kind="test_seed", ref_id="test_victim_idempotent_seed"
    )
    await session.commit()
    bank_before = await _get_bank_balance(session, chat_id)

    monkeypatch.setattr(daily_pick_service, "_rng", _ForcedChoiceRng(uid))

    first = await victim_service.run_victim(session, chat_id)
    bank_after_first = await _get_bank_balance(session, chat_id)

    second = await victim_service.run_victim(session, chat_id)
    bank_after_second = await _get_bank_balance(session, chat_id)

    assert first["winner"] == uid
    assert first["is_new"] is True
    assert second["winner"] == uid
    assert second["is_new"] is False

    # Банк списан на приз РОВНО один раз, повтор не переплачивает.
    assert bank_after_first == bank_before - victim_service.VICTIM_PRIZE
    assert bank_after_second == bank_after_first


@pytest.mark.asyncio
async def test_victim_prize_from_bank(session, monkeypatch):
    chat_id = -1009004002
    uid = 9004002
    await _ensure_user(session, uid, "Жертва")
    balance_before = await _fund(session, chat_id, uid)
    await _seed_daily_stat(session, chat_id, uid)
    await economy_service.credit_bank(
        session, chat_id, 10_000, kind="test_seed", ref_id="test_victim_prize_seed"
    )
    await session.commit()
    bank_before = await _get_bank_balance(session, chat_id)

    monkeypatch.setattr(daily_pick_service, "_rng", _ForcedChoiceRng(uid))

    result = await victim_service.run_victim(session, chat_id)

    assert result["prize"] == victim_service.VICTIM_PRIZE
    assert (
        await _get_user_balance(session, chat_id, uid)
        == balance_before + victim_service.VICTIM_PRIZE
    )
    assert await _get_bank_balance(session, chat_id) == bank_before - victim_service.VICTIM_PRIZE

    # Повтор с тем же ref_id (kind victim_prize), который использовал run_victim,
    # не начисляет второй раз — идемпотентность самого pay_from_bank (D-05).
    ref_id = f"victim:{chat_id}:{result['day_msk']}"
    paid_again = await economy_service.pay_from_bank(
        session, chat_id, uid, victim_service.VICTIM_PRIZE, kind="victim_prize", ref_id=ref_id
    )
    await session.commit()
    assert paid_again == 0


@pytest.mark.asyncio
async def test_victim_prize_capped_by_bank(session, monkeypatch):
    chat_id = -1009004003
    uid = 9004003
    await _ensure_user(session, uid, "Жертва")
    balance_before = await _fund(session, chat_id, uid)
    await _seed_daily_stat(session, chat_id, uid)
    await economy_service.credit_bank(
        session, chat_id, 100, kind="test_seed", ref_id="test_victim_cap_seed"
    )
    await session.commit()

    monkeypatch.setattr(daily_pick_service, "_rng", _ForcedChoiceRng(uid))

    result = await victim_service.run_victim(session, chat_id)

    # min(228, банк=100) — cap банком, никогда не минус.
    assert result["prize"] == 100
    assert await _get_user_balance(session, chat_id, uid) == balance_before + 100
    assert await _get_bank_balance(session, chat_id) == 0


# --- Дебафф: удвоенная комиссия перевода (D-06) ------------------------------


@pytest.mark.asyncio
async def test_transfer_fee_doubled_for_victim(session):
    chat_id = -1009004004
    sender, receiver = 9004004, 9004005
    await _ensure_user(session, sender, "Отправитель")
    await _ensure_user(session, receiver, "Получатель")
    await _fund(session, chat_id, sender)
    await _fund(session, chat_id, receiver)

    amount = 100

    bank_before = await _get_bank_balance(session, chat_id)
    # default kwarg (fee_multiplier=1.0) сохраняет старое поведение.
    await economy_service.transfer_with_fee(
        session, chat_id, sender, receiver, amount, "test_fee_normal"
    )
    normal_fee = (await _get_bank_balance(session, chat_id)) - bank_before

    bank_before_doubled = await _get_bank_balance(session, chat_id)
    await economy_service.transfer_with_fee(
        session, chat_id, sender, receiver, amount, "test_fee_doubled", fee_multiplier=2.0
    )
    doubled_fee = (await _get_bank_balance(session, chat_id)) - bank_before_doubled

    assert doubled_fee == normal_fee * 2


# --- is_active_victim: окно дебаффа по дню/expires_at ------------------------


@pytest.mark.asyncio
async def test_is_active_victim_window(session, monkeypatch):
    chat_id = -1009004006
    uid = 9004006
    await _ensure_user(session, uid, "Жертва")
    await _fund(session, chat_id, uid)
    await _seed_daily_stat(session, chat_id, uid)
    await session.commit()

    monkeypatch.setattr(daily_pick_service, "_rng", _ForcedChoiceRng(uid))
    await victim_service.run_victim(session, chat_id)

    assert await victim_service.is_active_victim(session, chat_id, uid) is True

    # Сдвигаем "сегодня" на следующий день — дебафф вчерашней жертвы уже снят.
    tomorrow = daily_pick_service._today_msk() + timedelta(days=1)
    monkeypatch.setattr(daily_pick_service, "_today_msk", lambda: tomorrow)

    assert await victim_service.is_active_victim(session, chat_id, uid) is False


# --- Новый MSK-день = новый пик (VICTIM-02/Pitfall 4) ------------------------


@pytest.mark.asyncio
async def test_new_day_new_victim(session, monkeypatch):
    chat_id = -1009004007
    uid1, uid2 = 9004007, 9004008
    await _ensure_user(session, uid1, "Жертва1")
    await _ensure_user(session, uid2, "Жертва2")
    await _fund(session, chat_id, uid1)
    await _fund(session, chat_id, uid2)
    await _seed_daily_stat(session, chat_id, uid1)
    await _seed_daily_stat(session, chat_id, uid2)
    await session.commit()

    monkeypatch.setattr(daily_pick_service, "_rng", _ForcedChoiceRng(uid1))
    first = await victim_service.run_victim(session, chat_id)
    assert first["winner"] == uid1
    assert first["is_new"] is True

    tomorrow = daily_pick_service._today_msk() + timedelta(days=1)
    monkeypatch.setattr(daily_pick_service, "_today_msk", lambda: tomorrow)
    monkeypatch.setattr(daily_pick_service, "_rng", _ForcedChoiceRng(uid2))

    second = await victim_service.run_victim(session, chat_id)
    assert second["winner"] == uid2
    assert second["is_new"] is True
    assert second["day_msk"] == tomorrow


# --- Хендлер /victim: тег после коммита приза (D-10) -------------------------


@pytest.mark.asyncio
async def test_victim_handler_grants_tag(session, bot, monkeypatch):
    chat_id = -1009004009
    uid = 9004009
    await _ensure_user(session, uid, "Жертва")
    await _fund(session, chat_id, uid)
    await _seed_daily_stat(session, chat_id, uid)
    await economy_service.credit_bank(
        session, chat_id, 10_000, kind="test_seed", ref_id="test_victim_handler_seed"
    )
    await session.commit()

    monkeypatch.setattr(daily_pick_service, "_rng", _ForcedChoiceRng(uid))
    grant_title_mock = AsyncMock()
    monkeypatch.setattr(victim_handlers.tag_service, "grant_title", grant_title_mock)

    message = _fake_message(chat_id, uid, "Жертва", "/victim")
    await victim_handlers.victim_command(message, session, bot)

    grant_title_mock.assert_awaited_once()
    call = grant_title_mock.await_args
    assert call.args[0] is bot
    assert call.args[2] == chat_id
    assert call.args[3] == uid
    assert call.kwargs["source"] == "victim"
    assert call.kwargs["expires_at"] is not None


# --- WR-05 (05-REVIEW.md): explicit rollback on DB-уровневой ошибке grant_title --


@pytest.mark.asyncio
async def test_victim_handler_rolls_back_session_on_grant_title_db_error(session, bot, monkeypatch):
    """grant_title может упасть DB-уровневой ошибкой (не только Telegram
    API, который вообще не трогает сессию) — раньше except-блок в
    bot/handlers/victim.py не делал явный session.rollback(), полагаясь на
    неявную подчистку при закрытии сессии вместо явной rollback-дисциплины,
    принятой в проекте для DB-уровневых исключений. Проверяем, что
    session.rollback() реально вызывается (не просто "хендлер не упал")."""
    chat_id = -1009004010
    uid = 9004010
    await _ensure_user(session, uid, "Жертва")
    await _fund(session, chat_id, uid)
    await _seed_daily_stat(session, chat_id, uid)
    await economy_service.credit_bank(
        session, chat_id, 10_000, kind="test_seed", ref_id="test_victim_handler_rollback_seed"
    )
    await session.commit()

    monkeypatch.setattr(daily_pick_service, "_rng", _ForcedChoiceRng(uid))
    monkeypatch.setattr(
        victim_handlers.tag_service,
        "grant_title",
        AsyncMock(side_effect=IntegrityError("insert", {}, Exception("boom"))),
    )

    rollback_mock = AsyncMock(wraps=session.rollback)
    monkeypatch.setattr(session, "rollback", rollback_mock)

    message = _fake_message(chat_id, uid, "Жертва", "/victim")
    await victim_handlers.victim_command(message, session, bot)

    rollback_mock.assert_awaited_once()
    message.answer.assert_awaited_once()

    message.answer.assert_awaited_once()


# --- expires_at не продлевается повторным /victim в тот же день -------------


@pytest.mark.asyncio
async def test_run_victim_expires_at_not_extended_on_replay(session, monkeypatch):
    """run_victim пересчитывает `expires_at = utcnow() + 24h` НА КАЖДОМ
    вызове (bot/services/victim_service.py строка 67) и всегда передаёт его
    в get_or_set_pick, но контракт get_or_set_pick — игнорировать
    `expires_at` при уже существующей строке (docstring, daily_pick_service.py
    строки 50-52). Форсируем ОЧЕНЬ разные "текущие моменты" между первым и
    вторым вызовом (monkeypatch victim_service.datetime.utcnow), чтобы если
    бы игнорирование когда-нибудь сломалось (например, on_conflict_do_nothing
    заменили апдейтом или убрали ранний return), stored expires_at второго
    вызова резко отличался бы от первого — тест это гарантированно поймает."""
    chat_id = -1009004011
    uid = 9004011
    await _ensure_user(session, uid, "Жертва")
    await _fund(session, chat_id, uid)
    await _seed_daily_stat(session, chat_id, uid)
    await economy_service.credit_bank(
        session, chat_id, 10_000, kind="test_seed", ref_id="test_victim_expires_replay_seed"
    )
    await session.commit()

    monkeypatch.setattr(daily_pick_service, "_rng", _ForcedChoiceRng(uid))

    fake_now = _FixedUtcNow(datetime(2026, 1, 1, 0, 0, 0))
    monkeypatch.setattr(victim_service, "datetime", fake_now)

    first = await victim_service.run_victim(session, chat_id)
    assert first["is_new"] is True
    assert first["expires_at"] == fake_now.current + timedelta(hours=victim_service.VICTIM_DEBUFF_HOURS)

    # "Текущий момент" второго вызова — на 100 дней позже первого. Если бы
    # expires_at реально продлевался при replay, второй stored expires_at
    # отличался бы от первого ровно на эти 100 дней.
    fake_now.current = datetime(2026, 4, 11, 0, 0, 0)

    second = await victim_service.run_victim(session, chat_id)
    assert second["is_new"] is False
    assert second["expires_at"] == first["expires_at"]


# --- Откат ДО commit при ошибке между RNG-ролом и коммитом (rollback-on-error) --


@pytest.mark.asyncio
async def test_run_victim_rolls_back_cleanly_on_payout_error(session, monkeypatch):
    """run_victim делает get_or_set_pick (RNG-ролл + INSERT, не закоммичено)
    -> pay_from_bank -> SELECT stored_expires_at -> ЕДИНСТВЕННЫЙ
    session.commit() в конце, без try/except вокруг всего блока
    (bot/services/victim_service.py строки 62-96). Форсируем
    economy_service.pay_from_bank (импортированный в victim_service) на
    RuntimeError ПОСЛЕ того, как RNG уже выбрал жертву и get_or_set_pick уже
    вставил (не закоммитил) строку — и проверяем ДВЕ вещи:

    1) session.commit() физически НЕ достигается (мокаем сам `session.commit`
       и проверяем, что его не вызвали) — это и есть контракт "RNG-ролл и
       выплата коммитятся строго атомарно вместе", а не благое пожелание.
    2) Откат незакоммиченной работы run_victim (форма того, что реально
       делает `async with SessionLocal()` у DbSessionMiddleware/
       register_daily_autopost._job при закрытии сессии на этой ошибке — там
       это session.close()/полный rollback сессии) снимает пик полностью:
       день не "сожжён", свежий (рабочий) вызов run_victim всё ещё выбирает
       жертву и платит приз, а не молча пропускает выплату из-за залипшего
       пика.

    Откат здесь сделан через `session.begin_nested()` (SAVEPOINT) вокруг
    самого вызова run_victim, а не через прямой `session.rollback()` —
    фикстура `session` в conftest.py держит весь тест в ОДНОЙ обёрточной
    транзакции connection-уровня, и голый `session.rollback()` обрывает её
    целиком (включая уже закоммиченные ранее в тесте seed-данные, причём
    необратимо для всех дальнейших commit() в этом тесте — ловушка именно
    этого тестового окружения, не имеющая отношения к проверяемому
    контракту). SAVEPOINT — стандартный SQLAlchemy-способ откатить РОВНО
    работу run_victim, не трогая ни seed-данные до него, ни изоляцию теста."""
    chat_id = -1009004013
    uid = 9004013
    await _ensure_user(session, uid, "Жертва")
    balance_before = await _fund(session, chat_id, uid)
    await _seed_daily_stat(session, chat_id, uid)
    await economy_service.credit_bank(
        session, chat_id, 10_000, kind="test_seed", ref_id="test_victim_rollback_seed"
    )
    await session.commit()
    bank_before = await _get_bank_balance(session, chat_id)

    real_pay_from_bank = economy_service.pay_from_bank
    monkeypatch.setattr(daily_pick_service, "_rng", _ForcedChoiceRng(uid))
    monkeypatch.setattr(
        victim_service.economy_service,
        "pay_from_bank",
        AsyncMock(side_effect=RuntimeError("boom: сбой БД/сети в момент выплаты")),
    )
    commit_mock = AsyncMock(wraps=session.commit)
    monkeypatch.setattr(session, "commit", commit_mock)

    with pytest.raises(RuntimeError):
        async with session.begin_nested():
            await victim_service.run_victim(session, chat_id)

    # Контракт "атомарно вместе": исключение из pay_from_bank пробрасывается
    # ДО того, как run_victim успевает дойти до своего единственного commit().
    commit_mock.assert_not_awaited()

    # SAVEPOINT откатил незакоммиченный пик и всё, что успел сделать
    # run_victim, — день не "сожжён", деньги не сдвинуты.
    pending_pick_winner = (
        await session.execute(
            select(DailyPick.winner_user_id).where(
                DailyPick.chat_id == chat_id, DailyPick.kind == "victim"
            )
        )
    ).scalar_one_or_none()
    assert pending_pick_winner is None
    assert await _get_user_balance(session, chat_id, uid) == balance_before
    assert await _get_bank_balance(session, chat_id) == bank_before

    # Рабочий pay_from_bank восстановлен — следующий вызов run_victim всё ещё
    # выбирает жертву и платит приз, а не молча пропускает выплату.
    monkeypatch.setattr(victim_service.economy_service, "pay_from_bank", real_pay_from_bank)

    retry = await victim_service.run_victim(session, chat_id)
    assert retry["winner"] == uid
    assert retry["is_new"] is True
    assert retry["prize"] == victim_service.VICTIM_PRIZE
    assert await _get_user_balance(session, chat_id, uid) == balance_before + victim_service.VICTIM_PRIZE
