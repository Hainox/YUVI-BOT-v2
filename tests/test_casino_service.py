"""Интеграционные тесты casino_service против живого Postgres (фикстура
`session` из tests/conftest.py — транзакция-на-тест). Доказывают фундамент
казино (04.1-01): идемпотентность раунда по `idem_key` (повтор не двигает
деньги повторно и возвращает тот же сохранённый исход), D-06 (выигрыш
урезается до остатка chat_bank, банк никогда не уходит в минус), D-04/D-05
(лимиты минимальной/максимальной ставки — до любого движения денег), D-03
(точные формулы: коинфлип 1.98x, дайс mult=(1-0.02)/win_prob, рулетка
европейская number 36x / color-parity-half 2x / dozen 3x).

Все исходы форсируются через RNG-seam `casino_service._rng`
(`secrets.SystemRandom`-совместимый объект, monkeypatched в тестах) — никогда
не проверяем реальную случайность.

casino_service сам делает session.commit() там, где это описано в его
контракте — совместимо с фикстурой session благодаря join-savepoint режиму
SQLAlchemy 2.0 (тот же паттерн уже проверен в test_markets_service.py).
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from bot.config import settings
from bot.services import casino_service
from bot.services import economy_service
from common.models.casino_game import CasinoGame
from common.models.chat_bank import ChatBank
from common.models.user import User
from common.models.user_balance import UserBalance


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


async def _get_casino_game(session, user_id: int, idem_key: str) -> CasinoGame:
    return (
        await session.execute(
            select(CasinoGame).where(
                CasinoGame.user_id == user_id, CasinoGame.idem_key == idem_key
            )
        )
    ).scalar_one()


class _ForcedRng:
    """Тестовый RNG-стаб, monkeypatched вместо `casino_service._rng`.

    Позволяет форсировать детерминированный исход коинфлипа/кости/рулетки
    вместо реальной случайности `secrets.SystemRandom`.
    """

    def __init__(self, choice_value=None, randint_value: int | None = None):
        self._choice_value = choice_value
        self._randint_value = randint_value

    def choice(self, seq):
        if self._choice_value is not None:
            return self._choice_value
        return seq[0]

    def randint(self, a: int, b: int) -> int:
        if self._randint_value is not None:
            return self._randint_value
        return a


# --- Лимиты ставок (D-04/D-05) -----------------------------------------------


@pytest.mark.asyncio
async def test_bet_below_min_rejected(session):
    chat_id = -100900001
    user_id = 900001
    await _ensure_user(session, user_id)
    balance_before = await _fund(session, chat_id, user_id)

    with pytest.raises(casino_service.InvalidBet):
        await casino_service.play_coinflip(
            session, chat_id, user_id, settings.casino_min_bet - 1, "heads", "test_min_bet_reject"
        )

    assert await _get_user_balance(session, chat_id, user_id) == balance_before
    games = (
        await session.execute(select(CasinoGame).where(CasinoGame.user_id == user_id))
    ).scalars().all()
    assert games == []


@pytest.mark.asyncio
async def test_bet_above_balance_rejected(session):
    chat_id = -100900002
    user_id = 900002
    await _ensure_user(session, user_id)
    balance_before = await _fund(session, chat_id, user_id)

    with pytest.raises((casino_service.InvalidBet, economy_service.InsufficientFunds)):
        await casino_service.play_coinflip(
            session, chat_id, user_id, balance_before + 1000, "heads", "test_above_balance_reject"
        )

    assert await _get_user_balance(session, chat_id, user_id) == balance_before
    games = (
        await session.execute(select(CasinoGame).where(CasinoGame.user_id == user_id))
    ).scalars().all()
    assert games == []


# --- Коинфлип (D-03: 1.98x) --------------------------------------------------


@pytest.mark.asyncio
async def test_coinflip_win_pays_1_98x(session, monkeypatch):
    chat_id = -100900003
    user_id = 900003
    await _ensure_user(session, user_id)
    balance_before = await _fund(session, chat_id, user_id)
    # банк надо наполнить, иначе выигрыш урежется D-06 — не то, что здесь тестируем
    await economy_service.credit_bank(
        session, chat_id, 100_000, kind="test_seed", ref_id="test_coinflip_seed_bank"
    )
    await session.commit()

    monkeypatch.setattr(casino_service, "_rng", _ForcedRng(choice_value="heads"))

    bet = 100
    result = await casino_service.play_coinflip(
        session, chat_id, user_id, bet, "heads", "test_coinflip_win"
    )

    expected_payout = int(bet * casino_service.COINFLIP_MULT)
    assert result["payout"] == expected_payout
    assert await _get_user_balance(session, chat_id, user_id) == balance_before - bet + expected_payout

    game = await _get_casino_game(session, user_id, "test_coinflip_win")
    assert game.payout == expected_payout
    assert game.bet == bet
    assert game.game == "coinflip"


@pytest.mark.asyncio
async def test_coinflip_loss_keeps_stake_in_bank(session, monkeypatch):
    chat_id = -100900004
    user_id = 900004
    await _ensure_user(session, user_id)
    balance_before = await _fund(session, chat_id, user_id)
    bank_before = await _get_bank_balance(session, chat_id)

    monkeypatch.setattr(casino_service, "_rng", _ForcedRng(choice_value="tails"))

    bet = 50
    result = await casino_service.play_coinflip(
        session, chat_id, user_id, bet, "heads", "test_coinflip_loss"
    )

    assert result["payout"] == 0
    assert await _get_user_balance(session, chat_id, user_id) == balance_before - bet
    assert await _get_bank_balance(session, chat_id) == bank_before + bet


# --- Дайс (D-03: mult=(1-0.02)/win_prob) -------------------------------------


@pytest.mark.asyncio
async def test_dice_multiplier_matches_formula(session, monkeypatch):
    chat_id = -100900005
    user_id = 900005
    await _ensure_user(session, user_id)
    balance_before = await _fund(session, chat_id, user_id)
    await economy_service.credit_bank(
        session, chat_id, 100_000, kind="test_seed", ref_id="test_dice_seed_bank"
    )
    await session.commit()

    target = 50
    direction = "under"
    win_prob = (target - 1) / 100
    # r < target => under wins; форсируем r=1 (гарантированный under-win)
    monkeypatch.setattr(casino_service, "_rng", _ForcedRng(randint_value=1))

    bet = 100
    result = await casino_service.play_dice(
        session, chat_id, user_id, bet, target, direction, "test_dice_win"
    )

    expected_payout = int(bet * (1 - casino_service.DICE_HOUSE_EDGE) / win_prob)
    assert result["payout"] == expected_payout
    assert await _get_user_balance(session, chat_id, user_id) == balance_before - bet + expected_payout


# --- Рулетка (D-03: number 36x / color-parity-half 2x / dozen 3x) -----------


@pytest.mark.asyncio
async def test_roulette_number_pays_36x(session, monkeypatch):
    chat_id = -100900006
    user_id = 900006
    await _ensure_user(session, user_id)
    balance_before = await _fund(session, chat_id, user_id)
    await economy_service.credit_bank(
        session, chat_id, 100_000, kind="test_seed", ref_id="test_roulette_number_seed_bank"
    )
    await session.commit()

    monkeypatch.setattr(casino_service, "_rng", _ForcedRng(randint_value=7))

    bet = 10
    result = await casino_service.play_roulette(
        session, chat_id, user_id, bet, "number", 7, "test_roulette_number_win"
    )

    expected_payout = int(bet * casino_service.ROULETTE_NUMBER_MULT)
    assert result["payout"] == expected_payout
    assert await _get_user_balance(session, chat_id, user_id) == balance_before - bet + expected_payout


@pytest.mark.asyncio
async def test_roulette_color_pays_2x(session, monkeypatch):
    chat_id = -100900007
    user_id = 900007
    await _ensure_user(session, user_id)
    balance_before = await _fund(session, chat_id, user_id)
    await economy_service.credit_bank(
        session, chat_id, 100_000, kind="test_seed", ref_id="test_roulette_color_seed_bank"
    )
    await session.commit()

    # 1 — красное число в европейской рулетке
    monkeypatch.setattr(casino_service, "_rng", _ForcedRng(randint_value=1))

    bet = 10
    result = await casino_service.play_roulette(
        session, chat_id, user_id, bet, "color", "red", "test_roulette_color_win"
    )

    expected_payout = int(bet * casino_service.ROULETTE_EVEN_MULT)
    assert result["payout"] == expected_payout
    assert await _get_user_balance(session, chat_id, user_id) == balance_before - bet + expected_payout


@pytest.mark.asyncio
async def test_roulette_dozen_pays_3x(session, monkeypatch):
    chat_id = -100900008
    user_id = 900008
    await _ensure_user(session, user_id)
    balance_before = await _fund(session, chat_id, user_id)
    await economy_service.credit_bank(
        session, chat_id, 100_000, kind="test_seed", ref_id="test_roulette_dozen_seed_bank"
    )
    await session.commit()

    # 5 попадает в первую дюжину (1-12)
    monkeypatch.setattr(casino_service, "_rng", _ForcedRng(randint_value=5))

    bet = 10
    result = await casino_service.play_roulette(
        session, chat_id, user_id, bet, "dozen", 1, "test_roulette_dozen_win"
    )

    expected_payout = int(bet * casino_service.ROULETTE_DOZEN_MULT)
    assert result["payout"] == expected_payout
    assert await _get_user_balance(session, chat_id, user_id) == balance_before - bet + expected_payout


# --- Идемпотентность replay --------------------------------------------------


@pytest.mark.asyncio
async def test_idempotent_replay_same_idem_key(session, monkeypatch):
    chat_id = -100900009
    user_id = 900009
    await _ensure_user(session, user_id)
    balance_before = await _fund(session, chat_id, user_id)
    await economy_service.credit_bank(
        session, chat_id, 100_000, kind="test_seed", ref_id="test_idempotent_seed_bank"
    )
    await session.commit()

    monkeypatch.setattr(casino_service, "_rng", _ForcedRng(choice_value="heads"))

    idem_key = "test_idempotent_replay"
    bet = 100
    first = await casino_service.play_coinflip(session, chat_id, user_id, bet, "heads", idem_key)

    balance_after_first = await _get_user_balance(session, chat_id, user_id)

    second = await casino_service.play_coinflip(session, chat_id, user_id, bet, "heads", idem_key)

    assert second["payout"] == first["payout"]
    assert second["outcome"] == first["outcome"]

    # деньги не двинулись повторно
    assert await _get_user_balance(session, chat_id, user_id) == balance_after_first

    games = (
        await session.execute(
            select(CasinoGame).where(CasinoGame.user_id == user_id, CasinoGame.idem_key == idem_key)
        )
    ).scalars().all()
    assert len(games) == 1


# --- Bank cap (D-06) ----------------------------------------------------------


@pytest.mark.asyncio
async def test_payout_capped_to_bank_balance(session, monkeypatch):
    chat_id = -100900010
    user_id = 900010
    await _ensure_user(session, user_id)
    await _fund(session, chat_id, user_id)

    # Почти пустой банк — выигрыш точно превысит его остаток.
    small_bank = 5
    await economy_service.credit_bank(
        session, chat_id, small_bank, kind="test_seed", ref_id="test_bank_cap_seed_bank"
    )
    await session.commit()

    monkeypatch.setattr(casino_service, "_rng", _ForcedRng(choice_value="heads"))

    bet = 1000
    result = await casino_service.play_coinflip(
        session, chat_id, user_id, bet, "heads", "test_bank_cap_win"
    )

    # Полный payout был бы int(bet*1.98) = 1980, но банк после дебета ставки
    # (bet зачислен в банк ДО выплаты) не может позволить себе весь выигрыш.
    bank_balance_after = await _get_bank_balance(session, chat_id)
    assert bank_balance_after >= 0

    game = await _get_casino_game(session, user_id, "test_bank_cap_win")
    assert result["payout"] == game.payout
    # выигрыш урезан — не полная сумма 1980
    assert result["payout"] < int(bet * casino_service.COINFLIP_MULT)
