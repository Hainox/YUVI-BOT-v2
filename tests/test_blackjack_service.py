"""Интеграционные тесты блэкджека (04.1-03) против живого Postgres (фикстура
`session` из tests/conftest.py). Доказывают: чистую карточную логику
`blackjack_engine` (подсчёт очков с мягким тузом, натурал, доигровка дилера
с остановкой на soft 17 — D-03), стейтфул-раздачу `casino_service.start_blackjack`/
`blackjack_action` (колода/руки живут в `CasinoGame.state` JSONB, сервер
никогда не принимает карты от клиента — T-04.1-08), статус-переход
"active"->"settled" как гард идемпотентности повторного действия (T-04.1-09),
и авто-стенд просроченных раздач `resolve_blackjack_timeouts` (D-07/D-08,
T-04.1-10) — ставка никогда не замораживается навсегда.

Детерминированная колода форсируется через monkeypatch `casino_service._rng`
стабом, чей `.shuffle(deck)` переставляет 52-карточную колоду так, чтобы
`deck.pop()` (берёт с КОНЦА списка) отдавал карты строго в порядке,
заданном тестом — никогда не проверяем реальную случайность.
"""

from __future__ import annotations

from datetime import datetime
from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy import update

from bot.config import settings
from bot.services import blackjack_engine
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


async def _seed_bank(session, chat_id: int, amount: int, ref_id: str) -> None:
    await economy_service.credit_bank(session, chat_id, amount, kind="test_seed", ref_id=ref_id)
    await session.commit()


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


async def _reload(session, game_id: int) -> CasinoGame:
    return (
        await session.execute(select(CasinoGame).where(CasinoGame.id == game_id))
    ).scalar_one()


class _FixedDeckRng:
    """Тестовый RNG-стаб для `casino_service._rng` — `shuffle(deck)`
    переставляет переданную 52-карточную колоду так, чтобы `deck.pop()`
    (берёт с КОНЦА) отдавал карты СТРОГО в порядке `pop_sequence`, что бы
    ни осталось дальше (тесты никогда не добирают карт за пределы
    заданной последовательности)."""

    def __init__(self, pop_sequence: list[str]):
        self._pop_sequence = pop_sequence

    def shuffle(self, deck: list[str]) -> None:
        remaining = list(deck)
        for card in self._pop_sequence:
            remaining.remove(card)
        deck[:] = remaining + list(reversed(self._pop_sequence))


async def _set_turn_deadline_past(session, game_id: int) -> None:
    game = await _reload(session, game_id)
    state = dict(game.state)
    state["turn_deadline"] = (datetime.utcnow() - timedelta(seconds=5)).isoformat()
    game.state = state
    await session.commit()


# --- blackjack_engine (чистая логика, без DB) --------------------------------


def test_hand_value_and_soft_ace():
    value, soft = blackjack_engine.hand_value(["A♠", "K♥"])
    assert (value, soft) == (21, True)

    value, soft = blackjack_engine.hand_value(["A♠", "6♥"])
    assert (value, soft) == (17, True)  # soft 17

    value, soft = blackjack_engine.hand_value(["A♠", "6♥", "10♣"])
    assert (value, soft) == (17, False)  # туз перешёл в 1, чтобы не было перебора

    assert blackjack_engine.is_natural(["A♠", "K♥"]) is True
    assert blackjack_engine.is_natural(["9♠", "9♣"]) is False
    assert blackjack_engine.is_natural(["A♠", "K♥", "2♣"]) is False  # не 2 карты


def test_dealer_stands_on_soft_17():
    deck = ["9♠", "9♣"]
    final_deck, dealer_final = blackjack_engine.dealer_play(list(deck), ["A♠", "6♥"])
    assert dealer_final == ["A♠", "6♥"]  # soft 17 — дилер НЕ добирает (D-03 S17)
    assert final_deck == deck  # колода не тронута


# --- start_blackjack -----------------------------------------------------


@pytest.mark.asyncio
async def test_start_deals_two_each_and_debits(session, monkeypatch):
    chat_id = -100910001
    user_id = 910001
    await _ensure_user(session, user_id)
    balance_before = await _fund(session, chat_id, user_id)

    monkeypatch.setattr(casino_service, "_rng", _FixedDeckRng(["8♠", "4♠", "7♠", "5♠"]))

    bet = 100
    result = await casino_service.start_blackjack(
        session, chat_id, user_id, bet, "test_bj_start"
    )

    assert result["status"] == "active"
    assert await _get_user_balance(session, chat_id, user_id) == balance_before - bet

    game = await _get_casino_game(session, user_id, "test_bj_start")
    assert game.game == "blackjack"
    assert game.status == "active"
    assert len(game.state["player"]) == 2
    assert len(game.state["dealer"]) == 2
    assert isinstance(game.state["deck"], list)

    deadline = datetime.fromisoformat(game.state["turn_deadline"])
    expected = datetime.utcnow() + timedelta(seconds=casino_service.BLACKJACK_TURN_SECONDS)
    assert abs((deadline - expected).total_seconds()) < 5


@pytest.mark.asyncio
async def test_start_idempotent_on_idem_key(session, monkeypatch):
    chat_id = -100910002
    user_id = 910002
    await _ensure_user(session, user_id)
    balance_before = await _fund(session, chat_id, user_id)

    monkeypatch.setattr(casino_service, "_rng", _FixedDeckRng(["8♠", "4♠", "7♠", "5♠"]))

    idem_key = "test_bj_start_idem"
    bet = 100
    first = await casino_service.start_blackjack(session, chat_id, user_id, bet, idem_key)
    balance_after_first = await _get_user_balance(session, chat_id, user_id)

    second = await casino_service.start_blackjack(session, chat_id, user_id, bet, idem_key)

    assert second["status"] == first["status"]
    assert await _get_user_balance(session, chat_id, user_id) == balance_after_first
    assert balance_after_first == balance_before - bet

    games = (
        await session.execute(
            select(CasinoGame).where(CasinoGame.user_id == user_id, CasinoGame.idem_key == idem_key)
        )
    ).scalars().all()
    assert len(games) == 1


# --- Натурал / выигрыш / push / bust (D-03) ----------------------------------


@pytest.mark.asyncio
async def test_natural_pays_2_5x(session, monkeypatch):
    chat_id = -100910003
    user_id = 910003
    await _ensure_user(session, user_id)
    balance_before = await _fund(session, chat_id, user_id)
    await _seed_bank(session, chat_id, 100_000, "test_bj_natural_seed_bank")

    # p1=A, p2=K (натурал 21), d1=8, d2=4 (12, не натурал)
    monkeypatch.setattr(casino_service, "_rng", _FixedDeckRng(["A♠", "K♠", "8♠", "4♠"]))

    bet = 100
    result = await casino_service.start_blackjack(session, chat_id, user_id, bet, "test_bj_natural")

    expected_payout = int(bet * casino_service.BLACKJACK_NATURAL_MULT)
    assert result["status"] == "settled"
    assert result["payout"] == expected_payout
    assert result["outcome"]["result"] == "natural"
    assert await _get_user_balance(session, chat_id, user_id) == balance_before - bet + expected_payout

    game = await _get_casino_game(session, user_id, "test_bj_natural")
    assert game.status == "settled"
    assert game.payout == expected_payout


@pytest.mark.asyncio
async def test_regular_win_pays_2x(session, monkeypatch):
    chat_id = -100910004
    user_id = 910004
    await _ensure_user(session, user_id)
    balance_before = await _fund(session, chat_id, user_id)
    await _seed_bank(session, chat_id, 100_000, "test_bj_win_seed_bank")

    # p1=10,p2=10 (20, не натурал); d1=10,d2=5 (15); стенд -> дилер добирает 2 -> 17
    monkeypatch.setattr(casino_service, "_rng", _FixedDeckRng(["10♠", "10♣", "10♥", "5♠", "2♠"]))

    bet = 100
    started = await casino_service.start_blackjack(session, chat_id, user_id, bet, "test_bj_win")
    assert started["status"] == "active"

    result = await casino_service.blackjack_action(
        session, chat_id, started["id"], user_id, "stand"
    )

    expected_payout = int(bet * 2.0)
    assert result["outcome"]["result"] == "win"
    assert result["payout"] == expected_payout
    assert await _get_user_balance(session, chat_id, user_id) == balance_before - bet + expected_payout


@pytest.mark.asyncio
async def test_push_refunds_1x(session, monkeypatch):
    chat_id = -100910005
    user_id = 910005
    await _ensure_user(session, user_id)
    balance_before = await _fund(session, chat_id, user_id)
    await _seed_bank(session, chat_id, 100_000, "test_bj_push_seed_bank")

    # p1=9,p2=9 (18); d1=9,d2=9 (18, дилер стоит сразу — уже >=17)
    monkeypatch.setattr(casino_service, "_rng", _FixedDeckRng(["9♠", "9♣", "9♥", "9♦"]))

    bet = 100
    started = await casino_service.start_blackjack(session, chat_id, user_id, bet, "test_bj_push")

    result = await casino_service.blackjack_action(
        session, chat_id, started["id"], user_id, "stand"
    )

    assert result["outcome"]["result"] == "push"
    assert result["payout"] == bet
    assert await _get_user_balance(session, chat_id, user_id) == balance_before - bet + bet


@pytest.mark.asyncio
async def test_bust_loses(session, monkeypatch):
    chat_id = -100910006
    user_id = 910006
    await _ensure_user(session, user_id)
    balance_before = await _fund(session, chat_id, user_id)
    await _seed_bank(session, chat_id, 100_000, "test_bj_bust_seed_bank")

    # p1=10,p2=6 (16); d1=7,d2=5 (12, неважно); hit-карта=10 -> 26 перебор
    monkeypatch.setattr(casino_service, "_rng", _FixedDeckRng(["10♠", "6♠", "7♠", "5♠", "10♣"]))

    bet = 100
    started = await casino_service.start_blackjack(session, chat_id, user_id, bet, "test_bj_bust")

    result = await casino_service.blackjack_action(
        session, chat_id, started["id"], user_id, "hit"
    )

    assert result["outcome"]["result"] == "bust"
    assert result["payout"] == 0
    assert await _get_user_balance(session, chat_id, user_id) == balance_before - bet


# --- double (D-03) ------------------------------------------------------


@pytest.mark.asyncio
async def test_double_doubles_stake_one_card_then_stands(session, monkeypatch):
    chat_id = -100910007
    user_id = 910007
    await _ensure_user(session, user_id)
    balance_before = await _fund(session, chat_id, user_id)
    await _seed_bank(session, chat_id, 100_000, "test_bj_double_seed_bank")

    # p1=8,p2=4 (12); d1=6,d2=5 (11); double draws 9 -> player 21; dealer
    # добирает 6 -> 17 (стоит)
    monkeypatch.setattr(
        casino_service, "_rng", _FixedDeckRng(["8♠", "4♠", "6♠", "5♠", "9♠", "6♣"])
    )

    bet = 100
    started = await casino_service.start_blackjack(session, chat_id, user_id, bet, "test_bj_double")

    result = await casino_service.blackjack_action(
        session, chat_id, started["id"], user_id, "double"
    )

    bet_effective = bet * 2
    expected_payout = int(bet_effective * 2.0)  # win 2x на удвоенную ставку
    assert result["outcome"]["result"] == "win"
    assert result["payout"] == expected_payout
    # два debit (первый + double) + один payout
    assert (
        await _get_user_balance(session, chat_id, user_id)
        == balance_before - bet - bet + expected_payout
    )

    game = await _get_casino_game(session, user_id, "test_bj_double")
    assert len(game.outcome["player"]) == 3  # ровно одна добранная карта


# --- double: replay-защита (WR-02) -------------------------------------------


@pytest.mark.asyncio
async def test_double_raises_when_debit_replayed(session, monkeypatch):
    """WR-02 (04.1-REVIEW) regression: раньше возврат `_debit_stake` на ветке
    "double" не проверялся — если бы debit не применился (idem_key уже
    использован), раздача всё равно удвоилась бы БЕЗ фактического списания
    второй ставки. Форсируем этот сценарий монкипатчем `_debit_stake`."""
    chat_id = -100910014
    user_id = 910014
    await _ensure_user(session, user_id)
    balance_before = await _fund(session, chat_id, user_id)
    await _seed_bank(session, chat_id, 100_000, "test_bj_double_replay_seed_bank")

    monkeypatch.setattr(casino_service, "_rng", _FixedDeckRng(["8♠", "4♠", "6♠", "5♠", "9♠", "6♣"]))

    bet = 100
    started = await casino_service.start_blackjack(session, chat_id, user_id, bet, "test_bj_double_replay")

    async def _fake_debit_stake(session, chat_id, user_id, bet, idem_key):
        return False

    monkeypatch.setattr(casino_service, "_debit_stake", _fake_debit_stake)

    with pytest.raises(casino_service.CasinoError):
        await casino_service.blackjack_action(session, chat_id, started["id"], user_id, "double")

    # Раздача не settle'илась молча и ставка не удвоилась.
    game = await _reload(session, started["id"])
    assert game.status == "active"
    assert await _get_user_balance(session, chat_id, user_id) == balance_before - bet


# --- Идемпотентность действия на уже settled раздаче (T-04.1-09) ------------


@pytest.mark.asyncio
async def test_action_on_settled_hand_is_idempotent(session, monkeypatch):
    chat_id = -100910008
    user_id = 910008
    await _ensure_user(session, user_id)
    await _fund(session, chat_id, user_id)
    await _seed_bank(session, chat_id, 100_000, "test_bj_idem_action_seed_bank")

    monkeypatch.setattr(casino_service, "_rng", _FixedDeckRng(["A♠", "K♠", "8♠", "4♠"]))

    bet = 100
    started = await casino_service.start_blackjack(session, chat_id, user_id, bet, "test_bj_idem_action")
    assert started["status"] == "settled"

    balance_after_settle = await _get_user_balance(session, chat_id, user_id)

    repeat = await casino_service.blackjack_action(
        session, chat_id, started["id"], user_id, "stand"
    )

    assert repeat["payout"] == started["payout"]
    assert repeat["outcome"] == started["outcome"]
    assert await _get_user_balance(session, chat_id, user_id) == balance_after_settle


# --- Timeout auto-stand (D-07/D-08, T-04.1-10) -------------------------------


@pytest.mark.asyncio
async def test_timeout_auto_stands(session, monkeypatch):
    chat_id = -100910009
    user_id = 910009
    await _ensure_user(session, user_id)
    balance_before = await _fund(session, chat_id, user_id)
    await _seed_bank(session, chat_id, 100_000, "test_bj_timeout_seed_bank")

    # p1=10,p2=10 (20); d1=10,d2=5 (15); авто-стенд -> дилер добирает 2 -> 17
    monkeypatch.setattr(casino_service, "_rng", _FixedDeckRng(["10♠", "10♣", "10♥", "5♠", "2♠"]))

    bet = 100
    started = await casino_service.start_blackjack(session, chat_id, user_id, bet, "test_bj_timeout")
    assert started["status"] == "active"

    await _set_turn_deadline_past(session, started["id"])

    resolved_count = await casino_service.resolve_blackjack_timeouts(session)
    assert resolved_count >= 1

    game = await _reload(session, started["id"])
    assert game.status == "settled"
    expected_payout = int(bet * 2.0)
    assert game.payout == expected_payout
    assert game.outcome["result"] == "win"
    assert await _get_user_balance(session, chat_id, user_id) == balance_before - bet + expected_payout


@pytest.mark.asyncio
async def test_timeout_batch_does_not_clobber_concurrently_settled_row(session, monkeypatch):
    """WR-03 (04.1-REVIEW) regression: раньше батч-wide `FOR UPDATE` на ВСЕХ
    просроченных раздачах релизился уже после commit'a ПЕРВОЙ обработанной
    строки — застрявший в памяти `game_row` ВТОРОЙ строки мог перезаписать
    исход, который параллельный live `blackjack_action` успел settle'ить
    между первичным batch-SELECT'ом и тем моментом, когда цикл до неё
    добрался. Симулируем гонку без реальной конкурентности: во время
    обработки ПЕРВОЙ раздачи в батче (side effect внутри `_finalize_blackjack`)
    напрямую переводим ВТОРУЮ раздачу в status="settled" с заведомо другим
    исходом — как будто это сделал параллельный вызов. Батч (с фиксом)
    обязан пропустить уже не-active вторую раздачу, а не перезаписать её."""
    chat_id = -100910015
    user_a, user_b = 910015, 910016
    await _ensure_user(session, user_a)
    await _ensure_user(session, user_b)
    await _fund(session, chat_id, user_a)
    await _fund(session, chat_id, user_b)
    await _seed_bank(session, chat_id, 100_000, "test_bj_batch_race_seed_bank")

    monkeypatch.setattr(casino_service, "_rng", _FixedDeckRng(["10♠", "10♣", "10♥", "5♠", "2♠"]))
    game_a = await casino_service.start_blackjack(session, chat_id, user_a, 100, "test_bj_batch_race_a")
    await _set_turn_deadline_past(session, game_a["id"])

    monkeypatch.setattr(casino_service, "_rng", _FixedDeckRng(["9♠", "9♣", "9♥", "9♦"]))
    game_b = await casino_service.start_blackjack(session, chat_id, user_b, 100, "test_bj_batch_race_b")
    await _set_turn_deadline_past(session, game_b["id"])

    real_finalize = casino_service._finalize_blackjack
    processed_order: list[int] = []

    async def _finalize_with_race(session, game_row, player, dealer, outcome_name, payout, *, state=None):
        processed_order.append(game_row.id)
        if len(processed_order) == 1:
            other_id = game_b["id"] if game_row.id == game_a["id"] else game_a["id"]
            await session.execute(
                update(CasinoGame)
                .where(CasinoGame.id == other_id)
                .values(
                    status="settled",
                    payout=999999,
                    outcome={"result": "concurrent_live_win", "player": [], "dealer": []},
                )
            )
            await session.commit()
        await real_finalize(session, game_row, player, dealer, outcome_name, payout, state=state)

    monkeypatch.setattr(casino_service, "_finalize_blackjack", _finalize_with_race)

    resolved_count = await casino_service.resolve_blackjack_timeouts(session)

    # Только первая (реально ещё активная на момент своей обработки) раздача
    # прошла через _finalize_blackjack — вторая была пропущена re-check'ом.
    assert resolved_count == 1
    assert len(processed_order) == 1

    other_id = game_b["id"] if processed_order[0] == game_a["id"] else game_a["id"]
    other_row = await _reload(session, other_id)
    assert other_row.payout == 999999
    assert other_row.outcome["result"] == "concurrent_live_win"


# --- Bank cap (D-06) ----------------------------------------------------------


@pytest.mark.asyncio
async def test_payout_capped_to_bank(session, monkeypatch):
    chat_id = -100910010
    user_id = 910010
    await _ensure_user(session, user_id)
    await _fund(session, chat_id, user_id)

    small_bank = 5
    await _seed_bank(session, chat_id, small_bank, "test_bj_bank_cap_seed_bank")

    monkeypatch.setattr(casino_service, "_rng", _FixedDeckRng(["A♠", "K♠", "8♠", "4♠"]))

    bet = 1000
    result = await casino_service.start_blackjack(session, chat_id, user_id, bet, "test_bj_bank_cap")

    bank_balance_after = await _get_bank_balance(session, chat_id)
    assert bank_balance_after >= 0

    game = await _get_casino_game(session, user_id, "test_bj_bank_cap")
    assert result["payout"] == game.payout
    assert result["payout"] < int(bet * casino_service.BLACKJACK_NATURAL_MULT)
