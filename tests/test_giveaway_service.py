"""Тесты разового случайного розыгрыша (`bot/services/giveaway_service.py`,
версия 2.0, запрошено 2026-07-28) против живого Postgres (фикстура `session`,
форма tests/test_lottery_service.py/test_victim_service.py). Доказывают:

- Пустой пул кандидатов -> {winner: None, credited: False, total_candidates: 0},
  без ValueError из _rng.choice([]).
- Форсированный победитель реально начисляется (economy_service.credit).
- Приз минтится (economy_service.credit), НЕ капается банком чата (в отличие
  от victim_service's pay_from_bank) — маленький банк не урезает выигрыш.
- Replay одного и того же ref_id НЕ перевыбирает победителя (в отличие от
  daily_pick_service.get_or_set_pick — здесь идемпотентность по ref_id, не
  "раз в день"): второй вызов возвращает ПЕРВОГО победителя, даже если RNG
  между вызовами форсирован на другое значение.
- Два РАЗНЫХ ref_id в один день — независимые роллы (доказывает отсутствие
  daily_pick_service-семантики "раз в день").
- Единственный кандидат в пуле — работает без ошибок.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from bot.services import economy_service
from bot.services import giveaway_service
from common.models.chat_bank import ChatBank
from common.models.user import User
from common.models.user_balance import UserBalance


async def _ensure_user(session, user_id: int, first_name: str = "Тест") -> None:
    session.add(User(id=user_id, first_name=first_name))
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
    """Тестовый RNG-стаб, monkeypatched вместо `giveaway_service._rng`.
    Форсирует детерминированный результат `.choice(seq)` (форма
    test_lottery_service.py/test_victim_service.py)."""

    def __init__(self, choice_value):
        self._choice_value = choice_value

    def choice(self, seq):
        return self._choice_value


# --- Пустой пул кандидатов ---------------------------------------------------


@pytest.mark.asyncio
async def test_giveaway_no_participants_returns_none_winner(session):
    chat_id = -1009006001

    result = await giveaway_service.run_giveaway(session, chat_id, 100, ref_id="giveaway:test1")

    assert result == {
        "winner": None,
        "amount": 100,
        "credited": False,
        "total_candidates": 0,
    }


# --- Форсированный победитель реально начисляется ----------------------------


@pytest.mark.asyncio
async def test_giveaway_picks_and_credits_forced_winner(session, monkeypatch):
    chat_id = -1009006002
    uid1, uid2 = 9006002, 9006003
    await _ensure_user(session, uid1, "Первый")
    await _ensure_user(session, uid2, "Второй")
    balance_before = await economy_service.get_balance(session, chat_id, uid1)
    await economy_service.get_balance(session, chat_id, uid2)
    await session.commit()

    monkeypatch.setattr(giveaway_service, "_rng", _ForcedChoiceRng(uid1))

    result = await giveaway_service.run_giveaway(session, chat_id, 300, ref_id="giveaway:test2")

    assert result["winner"] == uid1
    assert result["credited"] is True
    assert result["total_candidates"] == 2
    assert await _get_user_balance(session, chat_id, uid1) == balance_before + 300


# --- Минт, не банк ------------------------------------------------------------


@pytest.mark.asyncio
async def test_giveaway_mints_not_bank_funded(session, monkeypatch):
    chat_id = -1009006004
    uid = 9006004
    await _ensure_user(session, uid, "Победитель")
    balance_before = await economy_service.get_balance(session, chat_id, uid)
    small_bank = 5
    await economy_service.credit_bank(
        session, chat_id, small_bank, kind="test_seed", ref_id="test_giveaway_bank_seed"
    )
    await session.commit()

    monkeypatch.setattr(giveaway_service, "_rng", _ForcedChoiceRng(uid))

    result = await giveaway_service.run_giveaway(session, chat_id, 1000, ref_id="giveaway:test3")

    assert result["credited"] is True
    # Полная сумма, не урезанная маленьким банком (не pay_from_bank).
    assert await _get_user_balance(session, chat_id, uid) == balance_before + 1000
    assert await _get_bank_balance(session, chat_id) == small_bank


# --- Replay не перевыбирает победителя ----------------------------------------


@pytest.mark.asyncio
async def test_giveaway_replayed_ref_id_returns_original_winner_not_reroll(session, monkeypatch):
    chat_id = -1009006005
    uid1, uid2 = 9006005, 9006006
    await _ensure_user(session, uid1, "Настоящий")
    await _ensure_user(session, uid2, "Призрак")
    balance1_before = await economy_service.get_balance(session, chat_id, uid1)
    await economy_service.get_balance(session, chat_id, uid2)
    await session.commit()

    monkeypatch.setattr(giveaway_service, "_rng", _ForcedChoiceRng(uid1))
    first = await giveaway_service.run_giveaway(session, chat_id, 200, ref_id="giveaway:replay")

    # Между вызовами "меняем" RNG — если бы это был настоящий переролл, второй
    # вызов выбрал бы uid2, а не вернул уже оплаченного uid1.
    monkeypatch.setattr(giveaway_service, "_rng", _ForcedChoiceRng(uid2))
    second = await giveaway_service.run_giveaway(session, chat_id, 200, ref_id="giveaway:replay")

    assert first["winner"] == uid1
    assert first["credited"] is True
    assert second["winner"] == uid1  # НЕ uid2 — идемпотентность по ref_id
    assert second["credited"] is False
    assert await _get_user_balance(session, chat_id, uid1) == balance1_before + 200


# --- Два разных ref_id — независимые роллы ------------------------------------


@pytest.mark.asyncio
async def test_giveaway_two_different_ref_ids_same_day_are_independent_rolls(session, monkeypatch):
    chat_id = -1009006007
    uid1, uid2 = 9006007, 9006008
    await _ensure_user(session, uid1, "Первый")
    await _ensure_user(session, uid2, "Второй")
    balance1_before = await economy_service.get_balance(session, chat_id, uid1)
    balance2_before = await economy_service.get_balance(session, chat_id, uid2)
    await session.commit()

    monkeypatch.setattr(giveaway_service, "_rng", _ForcedChoiceRng(uid1))
    first = await giveaway_service.run_giveaway(session, chat_id, 150, ref_id="giveaway:first")

    monkeypatch.setattr(giveaway_service, "_rng", _ForcedChoiceRng(uid2))
    second = await giveaway_service.run_giveaway(session, chat_id, 250, ref_id="giveaway:second")

    assert first["winner"] == uid1
    assert first["credited"] is True
    assert second["winner"] == uid2
    assert second["credited"] is True
    assert await _get_user_balance(session, chat_id, uid1) == balance1_before + 150
    assert await _get_user_balance(session, chat_id, uid2) == balance2_before + 250


# --- Единственный кандидат в пуле ---------------------------------------------


@pytest.mark.asyncio
async def test_giveaway_single_candidate_pool(session):
    chat_id = -1009006009
    uid = 9006009
    await _ensure_user(session, uid, "Одинокий")
    balance_before = await economy_service.get_balance(session, chat_id, uid)
    await session.commit()

    result = await giveaway_service.run_giveaway(session, chat_id, 50, ref_id="giveaway:solo")

    assert result["winner"] == uid
    assert result["credited"] is True
    assert result["total_candidates"] == 1
    assert await _get_user_balance(session, chat_id, uid) == balance_before + 50
