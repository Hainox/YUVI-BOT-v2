"""Интеграционные тесты autobattle_service (GACHA-06) против живого Postgres
(фикстура `session` из tests/conftest.py) — форма test_duel_service.py, читай
его докстринг за полным разбором WR-01/WR-04. Расписываю здесь только то, что
реально отличается от Duel:

- `_character_power`/`team_power` — формула силы коллекции (новая, нет
  готового аналога в базе), сумма по всей гача-коллекции игрока в чате.
- `win_probability` — взвешенная (не честная 50/50) монетка, зажатая в
  [5%, 95%] даже при экстремальном перевесе силы.
- accept_autobattle резолвится через `_rng.random()` (Decimal-порог против
  `win_probability`), не через `_rng.choice([...])` — тестовый RNG-стаб
  форсирует конкретное значение `.random()`, а не выбор из списка.
- Эскроу/резолв/рефанд через economy_service — буквально та же форма, что
  Duel (тот же порядок: строка FOR UPDATE первой, деньги потом; ставка
  челленджера не заходит в chat_bank до accept), поэтому соответствующие
  money-safety тесты (bank receives only fee, payout failure after RNG does
  not fabricate/double charge, ref_id collision across two battles, full
  refund even when bank drained) зеркалятся у Duel почти дословно.
"""

from __future__ import annotations

import math
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

import bot.handlers.autobattle as autobattle_handlers
from bot.config import settings
from bot.services import autobattle_service
from bot.services import economy_service
from common.models.autobattle import AutoBattle
from common.models.chat_bank import ChatBank
from common.models.gacha_collection import GachaCollection
from common.models.user import User
from common.models.user_balance import UserBalance


# --- Хелперы (форма test_duel_service.py) ------------------------------------


async def _ensure_user(session, user_id: int, first_name: str = "Тест") -> None:
    session.add(User(id=user_id, first_name=first_name))
    await session.flush()


async def _fund(session, chat_id: int, user_id: int) -> int:
    """Заводит кошелёк (стартовый бонус economy_start_bonus) и коммитит."""
    return await economy_service.get_balance(session, chat_id, user_id)


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


async def _get_autobattle(session, autobattle_id: int) -> AutoBattle:
    return (
        await session.execute(select(AutoBattle).where(AutoBattle.id == autobattle_id))
    ).scalar_one()


async def _seed_character(
    session, chat_id: int, user_id: int, char_id: str, stars: int = 1, farm_level: int = 1
) -> None:
    session.add(
        GachaCollection(
            chat_id=chat_id,
            user_id=user_id,
            char_id=char_id,
            stars=stars,
            farm_level=farm_level,
            copies=1,
        )
    )
    await session.flush()


class _ForcedRandomRng:
    """Тестовый RNG-стаб, monkeypatched вместо `autobattle_service._rng`.
    Форсирует детерминированное `.random()` вместо реальной случайности —
    `accept_autobattle` сравнивает `Decimal(_rng.random())` с
    `win_probability(...)`, поэтому здесь (в отличие от Duel'евского
    `_ForcedChoiceRng.choice`) нужен именно порог, не прямой выбор победителя."""

    def __init__(self, value: float):
        self._value = value

    def random(self) -> float:
        return self._value


def _fake_message(
    chat_id: int,
    user_id: int,
    first_name: str,
    text: str,
    *,
    message_id: int = 1,
    reply_to_message=None,
    entities=None,
):
    """Минимальный aiogram-подобный Message для тестов тонких хендлеров
    autobattle.py (форма test_duel_service.py::_fake_message)."""
    return SimpleNamespace(
        chat=SimpleNamespace(id=chat_id),
        from_user=SimpleNamespace(id=user_id, first_name=first_name),
        message_id=message_id,
        text=text,
        reply_to_message=reply_to_message,
        entities=entities,
        answer=AsyncMock(),
        reply=AsyncMock(),
    )


def _fake_reply_target(user_id: int, first_name: str):
    """SimpleNamespace достаточный для
    target_resolution_service.resolve_target's reply-ветки (`from_user.id`/
    `.first_name`/`.is_bot`)."""
    return SimpleNamespace(
        from_user=SimpleNamespace(id=user_id, first_name=first_name, is_bot=False)
    )


# --- Формула силы (GACHA-06, чистые юнит-тесты без сессии) -------------------


def test_character_power_formula():
    # R, 1 звезда, farm_level 1: база 10 * star_mult(1)=1 * farm_bonus(1)=1
    assert autobattle_service._character_power("R", 1, 1) == Decimal(10)
    # UUR, 5 звёзд (star_mult=2.0), farm_level 11 (farm_bonus=1+0.01*10=1.10)
    expected = Decimal(150) * Decimal("2.0") * Decimal("1.10")
    assert autobattle_service._character_power("UUR", 5, 11) == expected
    # Неизвестный тир -> база 0.
    assert autobattle_service._character_power("???", 3, 1) == Decimal(0)


def test_win_probability_both_zero_returns_half():
    assert autobattle_service.win_probability(0, 0) == Decimal("0.5")


def test_win_probability_proportional_mid_range():
    # 300 vs 100 -> raw 0.75, внутри [0.05, 0.95] -> без изменений.
    assert autobattle_service.win_probability(300, 100) == Decimal("0.75")


def test_win_probability_clamped_at_bounds():
    assert autobattle_service.win_probability(0, 100) == autobattle_service.MIN_WIN_PROBABILITY
    assert autobattle_service.win_probability(999_999, 1) == autobattle_service.MAX_WIN_PROBABILITY


# --- team_power (чтение живой GachaCollection) --------------------------------


@pytest.mark.asyncio
async def test_team_power_sums_collection_and_skips_unknown_char(session):
    chat_id = -100920001
    user_id = 920001
    await _ensure_user(session, user_id)

    await _seed_character(session, chat_id, user_id, "r_elis", stars=1, farm_level=1)
    await _seed_character(session, chat_id, user_id, "s_ignis", stars=2, farm_level=1)
    # Персонаж без записи в каталоге — defensive skip, не должен падать.
    await _seed_character(session, chat_id, user_id, "nonexistent_char", stars=1, farm_level=1)

    expected = int(
        autobattle_service._character_power("R", 1, 1)
        + autobattle_service._character_power("S", 2, 1)
    )
    power = await autobattle_service.team_power(session, chat_id, user_id)
    assert power == expected
    assert power > 0


@pytest.mark.asyncio
async def test_team_power_zero_for_empty_collection(session):
    chat_id = -100920002
    user_id = 920002
    await _ensure_user(session, user_id)
    assert await autobattle_service.team_power(session, chat_id, user_id) == 0


# --- create_autobattle (эскроу ставки челленджера) ----------------------------


@pytest.mark.asyncio
async def test_create_escrows_challenger_stake(session):
    chat_id = -100920010
    challenger_id, opponent_id = 920010, 920011
    await _ensure_user(session, challenger_id, "Челленджер")
    await _ensure_user(session, opponent_id, "Оппонент")
    challenger_before = await _fund(session, chat_id, challenger_id)
    await _fund(session, chat_id, opponent_id)
    bank_before = await _get_bank_balance(session, chat_id)

    stake = 100
    battle = await autobattle_service.create_autobattle(
        session, chat_id, challenger_id, opponent_id, stake, "test_create_autobattle_escrow"
    )

    assert battle.status == "pending"
    assert battle.opponent_id == opponent_id
    assert battle.challenger_id == challenger_id
    assert battle.stake == stake

    assert await _get_user_balance(session, chat_id, challenger_id) == challenger_before - stake
    # Ставка НЕ заходит в общий chat_bank, пока автобитва pending (WR-04,
    # та же форма, что Duel) — рефанд отмены/отклонения не зависит от банка.
    assert await _get_bank_balance(session, chat_id) == bank_before


@pytest.mark.asyncio
async def test_create_below_min_bet_raises(session):
    chat_id = -100920012
    challenger_id, opponent_id = 920012, 920013
    await _ensure_user(session, challenger_id)
    await _ensure_user(session, opponent_id)
    balance_before = await _fund(session, chat_id, challenger_id)
    await _fund(session, chat_id, opponent_id)

    with pytest.raises(autobattle_service.AutoBattleError):
        await autobattle_service.create_autobattle(
            session,
            chat_id,
            challenger_id,
            opponent_id,
            settings.casino_min_bet - 1,
            "test_create_autobattle_min_bet",
        )

    assert await _get_user_balance(session, chat_id, challenger_id) == balance_before


@pytest.mark.asyncio
async def test_create_self_battle_raises_and_does_not_escrow(session):
    """WR-01 (по образцу Duel): opponent_id == challenger_id должен
    отвергаться на уровне сервиса, не только в хендлере."""
    chat_id = -100920014
    challenger_id = 920014
    await _ensure_user(session, challenger_id)
    balance_before = await _fund(session, chat_id, challenger_id)

    with pytest.raises(autobattle_service.AutoBattleError):
        await autobattle_service.create_autobattle(
            session,
            chat_id,
            challenger_id,
            challenger_id,
            100,
            "test_create_autobattle_self",
        )

    assert await _get_user_balance(session, chat_id, challenger_id) == balance_before
    battles = (
        await session.execute(
            select(AutoBattle).where(AutoBattle.challenger_id == challenger_id)
        )
    ).scalars().all()
    assert battles == []


# --- accept_autobattle (сила -> взвешенная монетка + fee) --------------------


@pytest.mark.asyncio
async def test_accept_resolves_weighted_coinflip_with_fee(session, monkeypatch):
    chat_id = -100920020
    challenger_id, opponent_id = 920020, 920021
    await _ensure_user(session, challenger_id, "Челленджер")
    await _ensure_user(session, opponent_id, "Оппонент")
    challenger_before = await _fund(session, chat_id, challenger_id)
    opponent_before = await _fund(session, chat_id, opponent_id)

    stake = 100
    battle = await autobattle_service.create_autobattle(
        session, chat_id, challenger_id, opponent_id, stake, "test_accept_create"
    )

    # Обе стороны без коллекции -> win_probability(0, 0) == 0.5; r=0.0 < 0.5
    # форсирует победу челленджера.
    monkeypatch.setattr(autobattle_service, "_rng", _ForcedRandomRng(0.0))

    result = await autobattle_service.accept_autobattle(
        session, chat_id, battle.id, opponent_id, "test_accept_ref"
    )

    expected_fee = max(1, math.ceil(2 * stake * settings.transfer_fee_pct))
    expected_pot = 2 * stake - expected_fee

    assert result["status"] == "resolved"
    assert result["winner_id"] == challenger_id
    assert result["loser_id"] == opponent_id
    assert result["fee"] == expected_fee
    assert result["pot"] == expected_pot
    assert result["challenger_power"] == 0
    assert result["opponent_power"] == 0

    battle_row = await _get_autobattle(session, battle.id)
    assert battle_row.status == "resolved"
    assert battle_row.winner_id == challenger_id
    assert battle_row.loser_id == opponent_id
    assert battle_row.fee == expected_fee
    assert battle_row.challenger_power == 0
    assert battle_row.opponent_power == 0

    assert (
        await _get_user_balance(session, chat_id, challenger_id)
        == challenger_before - stake + expected_pot
    )
    assert await _get_user_balance(session, chat_id, opponent_id) == opponent_before - stake


@pytest.mark.asyncio
async def test_accept_autobattle_bank_receives_only_fee(session, monkeypatch):
    chat_id = -100920022
    challenger_id, opponent_id = 920022, 920023
    await _ensure_user(session, challenger_id)
    await _ensure_user(session, opponent_id)
    await _fund(session, chat_id, challenger_id)
    await _fund(session, chat_id, opponent_id)
    bank_before = await _get_bank_balance(session, chat_id)

    stake = 100
    battle = await autobattle_service.create_autobattle(
        session, chat_id, challenger_id, opponent_id, stake, "test_bank_fee_create"
    )

    monkeypatch.setattr(autobattle_service, "_rng", _ForcedRandomRng(0.0))
    result = await autobattle_service.accept_autobattle(
        session, chat_id, battle.id, opponent_id, "test_bank_fee_accept"
    )

    assert await _get_bank_balance(session, chat_id) == bank_before + result["fee"]


@pytest.mark.asyncio
async def test_outcome_respects_power_weighted_threshold(session, monkeypatch):
    """Исход резолвится ИСКЛЮЧИТЕЛЬНО через RNG-seam `_rng` сравнённый с
    `win_probability`, взвешенной реальной силой коллекции — форсируем
    сильного челленджера (UUR 5★) против пустого оппонента и проверяем обе
    стороны порога вокруг вычисленной вероятности."""
    chat_id = -100920024
    challenger_id, opponent_id = 920024, 920025
    await _ensure_user(session, challenger_id)
    await _ensure_user(session, opponent_id)
    await _fund(session, chat_id, challenger_id)
    await _fund(session, chat_id, opponent_id)

    await _seed_character(session, chat_id, challenger_id, "uur_astrea", stars=5, farm_level=1)

    challenger_power = await autobattle_service.team_power(session, chat_id, challenger_id)
    opponent_power = await autobattle_service.team_power(session, chat_id, opponent_id)
    assert challenger_power > 0
    assert opponent_power == 0
    p = autobattle_service.win_probability(challenger_power, opponent_power)
    assert p == autobattle_service.MAX_WIN_PROBABILITY  # экстремальный перевес -> зажато 95%

    stake = 50
    battle_win = await autobattle_service.create_autobattle(
        session, chat_id, challenger_id, opponent_id, stake, "test_threshold_create_win"
    )
    monkeypatch.setattr(autobattle_service, "_rng", _ForcedRandomRng(float(p) - 0.01))
    result_win = await autobattle_service.accept_autobattle(
        session, chat_id, battle_win.id, opponent_id, "test_threshold_accept_win"
    )
    assert result_win["winner_id"] == challenger_id
    assert result_win["challenger_power"] == challenger_power
    assert result_win["opponent_power"] == 0

    battle_lose = await autobattle_service.create_autobattle(
        session, chat_id, challenger_id, opponent_id, stake, "test_threshold_create_lose"
    )
    monkeypatch.setattr(autobattle_service, "_rng", _ForcedRandomRng(float(p) + 0.01))
    result_lose = await autobattle_service.accept_autobattle(
        session, chat_id, battle_lose.id, opponent_id, "test_threshold_accept_lose"
    )
    # Даже 95%-перевес НЕ гарантирует исход — оппонент всё же может выиграть
    # (r >= p), это и есть 5%-й "потолок" MAX_WIN_PROBABILITY.
    assert result_lose["winner_id"] == opponent_id


@pytest.mark.asyncio
async def test_accept_wrong_opponent_rejected(session):
    chat_id = -100920026
    challenger_id, opponent_id, intruder_id = 920026, 920027, 920028
    await _ensure_user(session, challenger_id)
    await _ensure_user(session, opponent_id)
    await _ensure_user(session, intruder_id)
    await _fund(session, chat_id, challenger_id)
    await _fund(session, chat_id, opponent_id)
    await _fund(session, chat_id, intruder_id)

    battle = await autobattle_service.create_autobattle(
        session, chat_id, challenger_id, opponent_id, 100, "test_wrong_opponent_create"
    )

    with pytest.raises(autobattle_service.AutoBattleError):
        await autobattle_service.accept_autobattle(
            session, chat_id, battle.id, intruder_id, "test_wrong_opponent_accept"
        )

    battle_row = await _get_autobattle(session, battle.id)
    assert battle_row.status == "pending"


@pytest.mark.asyncio
async def test_accept_idempotent_on_resolved(session, monkeypatch):
    chat_id = -100920030
    challenger_id, opponent_id = 920030, 920031
    await _ensure_user(session, challenger_id)
    await _ensure_user(session, opponent_id)
    await _fund(session, chat_id, challenger_id)
    await _fund(session, chat_id, opponent_id)

    stake = 100
    battle = await autobattle_service.create_autobattle(
        session, chat_id, challenger_id, opponent_id, stake, "test_idempotent_create"
    )

    monkeypatch.setattr(autobattle_service, "_rng", _ForcedRandomRng(0.0))

    first = await autobattle_service.accept_autobattle(
        session, chat_id, battle.id, opponent_id, "test_idempotent_accept_1"
    )
    balance_after_first = await _get_user_balance(session, chat_id, challenger_id)

    second = await autobattle_service.accept_autobattle(
        session, chat_id, battle.id, opponent_id, "test_idempotent_accept_2"
    )

    assert second["status"] == first["status"] == "resolved"
    assert second["winner_id"] == first["winner_id"]
    assert second["loser_id"] == first["loser_id"]
    assert second["fee"] == first["fee"]
    assert second["pot"] == first["pot"]

    # деньги не двинулись повторно
    assert await _get_user_balance(session, chat_id, challenger_id) == balance_after_first


@pytest.mark.asyncio
async def test_accept_autobattle_ref_id_collision_across_two_battles_leaves_second_pending(
    session, monkeypatch
):
    """По образцу test_duel_service.py::
    test_accept_duel_ref_id_collision_across_two_duels_leaves_second_pending —
    ref_id, случайно переиспользованный между ДВУМЯ РАЗНЫМИ pending-автобитвами
    с общим opponent_id. Первый accept резолвит battle A; второй accept с тем
    же ref_id на ещё pending battle B обязан поднять AutoBattleAlreadyResolved,
    не повредив battle B."""
    chat_id = -100920032
    challenger_a_id, challenger_b_id, opponent_id = 920032, 920033, 920034
    await _ensure_user(session, challenger_a_id, "ЧелленджерA")
    await _ensure_user(session, challenger_b_id, "ЧелленджерB")
    await _ensure_user(session, opponent_id, "Оппонент")
    await _fund(session, chat_id, challenger_a_id)
    await _fund(session, chat_id, challenger_b_id)
    await _fund(session, chat_id, opponent_id)

    stake = 100
    battle_a = await autobattle_service.create_autobattle(
        session, chat_id, challenger_a_id, opponent_id, stake, "test_ref_collision_create_a"
    )
    battle_b = await autobattle_service.create_autobattle(
        session, chat_id, challenger_b_id, opponent_id, stake, "test_ref_collision_create_b"
    )

    shared_ref = "test_ref_collision_shared"
    monkeypatch.setattr(autobattle_service, "_rng", _ForcedRandomRng(0.0))
    first = await autobattle_service.accept_autobattle(
        session, chat_id, battle_a.id, opponent_id, shared_ref
    )
    assert first["status"] == "resolved"

    opponent_balance_after_a = await _get_user_balance(session, chat_id, opponent_id)

    with pytest.raises(autobattle_service.AutoBattleAlreadyResolved):
        await autobattle_service.accept_autobattle(
            session, chat_id, battle_b.id, opponent_id, shared_ref
        )

    battle_b_row = await _get_autobattle(session, battle_b.id)
    assert battle_b_row.status == "pending"
    assert battle_b_row.winner_id is None
    assert await _get_user_balance(session, chat_id, opponent_id) == opponent_balance_after_a

    second = await autobattle_service.accept_autobattle(
        session, chat_id, battle_b.id, opponent_id, "test_ref_collision_fresh_b"
    )
    assert second["status"] == "resolved"


@pytest.mark.asyncio
async def test_accept_autobattle_payout_failure_after_rng_does_not_fabricate_or_double_charge(
    session, monkeypatch
):
    """По образцу test_duel_service.py::
    test_accept_duel_payout_failure_after_rng_does_not_fabricate_or_double_charge
    — форсируем economy_service.pay_from_bank на RuntimeError ПОСЛЕ RNG-ролла:
    исключение обязано пробрасываться неподменённым, битва не должна
    оказаться наполовину resolved, деньги — не фабриковаться и не теряться
    (эскроу оппонента + перевод ставки челленджера в банк УЖЕ реально
    произошли, это шаги ДО pay_from_bank). Без явного session.rollback() — та
    же причина, что у Duel (см. его докстринг о join_transaction_mode)."""
    chat_id = -100920036
    challenger_id, opponent_id = 920036, 920037
    await _ensure_user(session, challenger_id, "Челленджер")
    await _ensure_user(session, opponent_id, "Оппонент")
    await _fund(session, chat_id, challenger_id)
    opponent_before = await _fund(session, chat_id, opponent_id)

    stake = 100
    battle = await autobattle_service.create_autobattle(
        session, chat_id, challenger_id, opponent_id, stake, "test_atomic_rollback_create"
    )
    challenger_before = await _get_user_balance(session, chat_id, challenger_id)
    bank_before = await _get_bank_balance(session, chat_id)

    monkeypatch.setattr(autobattle_service, "_rng", _ForcedRandomRng(0.0))
    monkeypatch.setattr(
        economy_service,
        "pay_from_bank",
        AsyncMock(side_effect=RuntimeError("симулированный сбой БД после RNG-ролла")),
    )

    ref_id = "test_atomic_rollback_accept"
    with pytest.raises(RuntimeError):
        await autobattle_service.accept_autobattle(session, chat_id, battle.id, opponent_id, ref_id)

    battle_row = await _get_autobattle(session, battle.id)
    assert battle_row.status == "pending"
    assert battle_row.winner_id is None
    assert battle_row.fee is None

    assert await _get_user_balance(session, chat_id, opponent_id) == opponent_before - stake
    assert await _get_user_balance(session, chat_id, challenger_id) == challenger_before
    assert await _get_bank_balance(session, chat_id) == bank_before + 2 * stake


# --- decline_autobattle / cancel_autobattle (полный рефанд) ------------------


@pytest.mark.asyncio
async def test_decline_refunds_challenger(session):
    chat_id = -100920040
    challenger_id, opponent_id = 920040, 920041
    await _ensure_user(session, challenger_id)
    await _ensure_user(session, opponent_id)
    challenger_before = await _fund(session, chat_id, challenger_id)
    await _fund(session, chat_id, opponent_id)

    stake = 100
    battle = await autobattle_service.create_autobattle(
        session, chat_id, challenger_id, opponent_id, stake, "test_decline_create"
    )

    result = await autobattle_service.decline_autobattle(session, chat_id, battle.id, opponent_id)

    assert result["status"] == "declined"
    battle_row = await _get_autobattle(session, battle.id)
    assert battle_row.status == "declined"
    assert await _get_user_balance(session, chat_id, challenger_id) == challenger_before


@pytest.mark.asyncio
async def test_cancel_refunds_challenger(session):
    chat_id = -100920042
    challenger_id, opponent_id = 920042, 920043
    await _ensure_user(session, challenger_id)
    await _ensure_user(session, opponent_id)
    challenger_before = await _fund(session, chat_id, challenger_id)
    await _fund(session, chat_id, opponent_id)

    stake = 100
    battle = await autobattle_service.create_autobattle(
        session, chat_id, challenger_id, opponent_id, stake, "test_cancel_create"
    )

    result = await autobattle_service.cancel_autobattle(session, chat_id, battle.id, challenger_id)

    assert result["status"] == "cancelled"
    battle_row = await _get_autobattle(session, battle.id)
    assert battle_row.status == "cancelled"
    assert await _get_user_balance(session, chat_id, challenger_id) == challenger_before


@pytest.mark.asyncio
async def test_decline_refund_full_even_when_shared_bank_is_empty(session):
    """WR-04 (по образцу Duel): ставка челленджера не заходит в chat_bank,
    пока автобитва pending, поэтому рефанд гарантированно полный независимо
    от состояния банка."""
    chat_id = -100920044
    challenger_id, opponent_id = 920044, 920045
    await _ensure_user(session, challenger_id)
    await _ensure_user(session, opponent_id)
    challenger_before = await _fund(session, chat_id, challenger_id)
    await _fund(session, chat_id, opponent_id)

    stake = 100
    battle = await autobattle_service.create_autobattle(
        session, chat_id, challenger_id, opponent_id, stake, "test_drain_create"
    )
    assert await _get_bank_balance(session, chat_id) == 0

    result = await autobattle_service.decline_autobattle(session, chat_id, battle.id, opponent_id)

    assert result["refunded"] == stake
    assert await _get_user_balance(session, chat_id, challenger_id) == challenger_before


@pytest.mark.asyncio
async def test_decline_wrong_actor_rejected(session):
    chat_id = -100920046
    challenger_id, opponent_id, intruder_id = 920046, 920047, 920048
    await _ensure_user(session, challenger_id)
    await _ensure_user(session, opponent_id)
    await _ensure_user(session, intruder_id)
    await _fund(session, chat_id, challenger_id)
    await _fund(session, chat_id, opponent_id)
    await _fund(session, chat_id, intruder_id)

    battle = await autobattle_service.create_autobattle(
        session, chat_id, challenger_id, opponent_id, 100, "test_decline_wrong_actor_create"
    )

    with pytest.raises(autobattle_service.AutoBattleError):
        await autobattle_service.decline_autobattle(session, chat_id, battle.id, intruder_id)

    battle_row = await _get_autobattle(session, battle.id)
    assert battle_row.status == "pending"


# --- Хендлеры: тонкие /autobattle* команды + inline-кнопки -------------------


@pytest.mark.asyncio
async def test_autobattle_command_creates_with_keyboard(session):
    chat_id = -100920050
    challenger_id, opponent_id = 920050, 920051
    await _ensure_user(session, challenger_id, "Челленджер")
    await _ensure_user(session, opponent_id, "Оппонент")
    await _fund(session, chat_id, challenger_id)
    await _fund(session, chat_id, opponent_id)

    message = _fake_message(
        chat_id,
        challenger_id,
        "Челленджер",
        "/autobattle 100",
        reply_to_message=_fake_reply_target(opponent_id, "Оппонент"),
    )

    await autobattle_handlers.autobattle_command(message, session)

    message.answer.assert_awaited_once()
    _, kwargs = message.answer.call_args
    assert kwargs["reply_markup"] is not None
    battles = (
        await session.execute(select(AutoBattle).where(AutoBattle.chat_id == chat_id))
    ).scalars().all()
    assert len(battles) == 1
    assert battles[0].status == "pending"
    assert battles[0].stake == 100


@pytest.mark.asyncio
async def test_autobattle_command_self_battle_rejected(session):
    chat_id = -100920052
    challenger_id = 920052
    await _ensure_user(session, challenger_id, "Челленджер")
    await _fund(session, chat_id, challenger_id)

    message = _fake_message(
        chat_id,
        challenger_id,
        "Челленджер",
        "/autobattle 100",
        reply_to_message=_fake_reply_target(challenger_id, "Челленджер"),
    )

    await autobattle_handlers.autobattle_command(message, session)

    message.answer.assert_awaited_once_with("Нельзя вызвать на автобитву самого себя.")
    battles = (
        await session.execute(select(AutoBattle).where(AutoBattle.chat_id == chat_id))
    ).scalars().all()
    assert battles == []


@pytest.mark.asyncio
async def test_autobattle_accept_command_resolves(session, monkeypatch):
    chat_id = -100920054
    challenger_id, opponent_id = 920054, 920055
    await _ensure_user(session, challenger_id)
    await _ensure_user(session, opponent_id)
    await _fund(session, chat_id, challenger_id)
    await _fund(session, chat_id, opponent_id)

    battle = await autobattle_service.create_autobattle(
        session, chat_id, challenger_id, opponent_id, 100, "test_handler_accept_create"
    )
    monkeypatch.setattr(autobattle_service, "_rng", _ForcedRandomRng(0.0))

    message = _fake_message(chat_id, opponent_id, "Оппонент", f"/autobattle_accept {battle.id}")
    await autobattle_handlers.autobattle_accept_command(message, session)

    message.answer.assert_awaited_once()
    text = message.answer.call_args.args[0]
    assert f"#{battle.id}" in text
    battle_row = await _get_autobattle(session, battle.id)
    assert battle_row.status == "resolved"


@pytest.mark.asyncio
async def test_autobattle_decline_command_refunds(session):
    chat_id = -100920056
    challenger_id, opponent_id = 920056, 920057
    await _ensure_user(session, challenger_id)
    await _ensure_user(session, opponent_id)
    challenger_before = await _fund(session, chat_id, challenger_id)
    await _fund(session, chat_id, opponent_id)

    battle = await autobattle_service.create_autobattle(
        session, chat_id, challenger_id, opponent_id, 100, "test_handler_decline_create"
    )

    message = _fake_message(chat_id, opponent_id, "Оппонент", f"/autobattle_decline {battle.id}")
    await autobattle_handlers.autobattle_decline_command(message, session)

    message.answer.assert_awaited_once_with(
        f"Автобитва #{battle.id} отклонена, ставка возвращена."
    )
    assert await _get_user_balance(session, chat_id, challenger_id) == challenger_before


@pytest.mark.asyncio
async def test_autobattle_cancel_command_refunds(session):
    chat_id = -100920058
    challenger_id, opponent_id = 920058, 920059
    await _ensure_user(session, challenger_id)
    await _ensure_user(session, opponent_id)
    challenger_before = await _fund(session, chat_id, challenger_id)
    await _fund(session, chat_id, opponent_id)

    battle = await autobattle_service.create_autobattle(
        session, chat_id, challenger_id, opponent_id, 100, "test_handler_cancel_create"
    )

    message = _fake_message(chat_id, challenger_id, "Челленджер", f"/autobattle_cancel {battle.id}")
    await autobattle_handlers.autobattle_cancel_command(message, session)

    message.answer.assert_awaited_once_with(
        f"Автобитва #{battle.id} отменена, ставка возвращена."
    )
    assert await _get_user_balance(session, chat_id, challenger_id) == challenger_before


@pytest.mark.asyncio
async def test_autobattle_accept_callback_resolves_and_edits(session, monkeypatch):
    chat_id = -100920060
    challenger_id, opponent_id = 920060, 920061
    await _ensure_user(session, challenger_id)
    await _ensure_user(session, opponent_id)
    await _fund(session, chat_id, challenger_id)
    await _fund(session, chat_id, opponent_id)

    battle = await autobattle_service.create_autobattle(
        session, chat_id, challenger_id, opponent_id, 100, "test_callback_accept_create"
    )
    monkeypatch.setattr(autobattle_service, "_rng", _ForcedRandomRng(0.0))

    callback = SimpleNamespace(
        from_user=SimpleNamespace(id=opponent_id),
        message=SimpleNamespace(chat=SimpleNamespace(id=chat_id), edit_text=AsyncMock()),
        data=f"autobattle_accept:{battle.id}",
        id="cb1",
        answer=AsyncMock(),
    )

    await autobattle_handlers.autobattle_accept_callback(callback, session)

    callback.message.edit_text.assert_awaited_once()
    callback.answer.assert_awaited_once_with()
    battle_row = await _get_autobattle(session, battle.id)
    assert battle_row.status == "resolved"


@pytest.mark.asyncio
async def test_autobattle_decline_callback_refunds_and_edits(session):
    chat_id = -100920062
    challenger_id, opponent_id = 920062, 920063
    await _ensure_user(session, challenger_id)
    await _ensure_user(session, opponent_id)
    challenger_before = await _fund(session, chat_id, challenger_id)
    await _fund(session, chat_id, opponent_id)

    battle = await autobattle_service.create_autobattle(
        session, chat_id, challenger_id, opponent_id, 100, "test_callback_decline_create"
    )

    callback = SimpleNamespace(
        from_user=SimpleNamespace(id=opponent_id),
        message=SimpleNamespace(chat=SimpleNamespace(id=chat_id), edit_text=AsyncMock()),
        data=f"autobattle_decline:{battle.id}",
        id="cb2",
        answer=AsyncMock(),
    )

    await autobattle_handlers.autobattle_decline_callback(callback, session)

    callback.message.edit_text.assert_awaited_once_with(
        f"Автобитва #{battle.id} отклонена, ставка возвращена."
    )
    callback.answer.assert_awaited_once_with()
    assert await _get_user_balance(session, chat_id, challenger_id) == challenger_before
