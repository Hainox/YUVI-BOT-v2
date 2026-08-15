"""Integration tests for Arena Phase 5 settlement (`arena_service.settle_match`
+ its wiring into `arena_runtime_worker`'s 1-second tick loop) — against a
real Postgres (`session` fixture from the root `tests/conftest.py`; Arena's
existing suite in this directory tests pure-domain/mocked-session logic, but
settlement moves real money through `economy_service` and writes to five
different tables via raw upserts/UNIQUE-constraint idempotency, so a mock
can't meaningfully verify it — same rationale as `tests/test_duel_service.py`/
`tests/test_autobattle_service.py` elsewhere in this repo).

Covers, per FIGHTING_SPEC.md §6/§8 and FIGHTING_ROADMAP.md Phase 5's stated
"Проверка" list:
- decisive win: payout = pot - 5% fee, fee split 2.5%/2.5% chat_bank/arena
  fund, exact invariant payout+fee_chat+fee_fund == pot;
- technical_loss: same payout math as a decisive win for the winner, but the
  LOSER's rating penalty is -30 (not -20) and XP is 0 (not 100);
- draw: full refund to both sides, no fee, but both sides still get draw XP;
- both-disconnected (refund_required): full refund, no stats recorded at all;
- unequal stakes;
- idempotency (repeat settle on an already-settled match is a same-row no-op,
  no double payment);
- money-safety rollback (a failure partway through the multi-step payout
  leaves no partial state — same class of regression test as
  test_duel_service.py::test_accept_duel_payout_failure_after_rng_does_not_fabricate_or_double_charge);
- rating/XP/level-up math;
- the runtime worker actually calls settle_match when a real
  ArenaSessionService-produced terminal state is observed.
"""

from __future__ import annotations

from decimal import Decimal
from decimal import ROUND_HALF_UP
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from bot.services import arena_service
from bot.services import economy_service
from common.arena.config import ArenaConfig
from common.arena.schemas import FighterType
from common.models.arena import ArenaFighterProgress
from common.models.arena import ArenaFund
from common.models.arena import ArenaMatch
from common.models.arena import ArenaProfile
from common.models.chat_bank import ChatBank
from common.models.user import User
from common.models.user_balance import UserBalance

_CONFIG = ArenaConfig()


# --- Хелперы (форма test_duel_service.py/test_autobattle_service.py) --------


async def _ensure_user(session, user_id: int, first_name: str = "Тест") -> None:
    """Idempotent insert (форма test_api_duel.py::_ensure_user) — most tests
    in this file run inside the fixture's rolled-back transaction and never
    need this, but the one worker-wiring test below does real commits
    against the long-lived sandbox Postgres, so a plain INSERT would collide
    with its own leftovers on a repeat run."""
    stmt = (
        pg_insert(User)
        .values(id=user_id, first_name=first_name)
        .on_conflict_do_nothing(index_elements=["id"])
    )
    await session.execute(stmt)
    await session.flush()


async def _fund(session, chat_id: int, user_id: int) -> int:
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


async def _get_arena_fund_balance(session, chat_id: int) -> int:
    result = await session.execute(select(ArenaFund.balance).where(ArenaFund.chat_id == chat_id))
    return result.scalar_one_or_none() or 0


async def _get_match(session, match_id: int) -> ArenaMatch:
    return (
        await session.execute(select(ArenaMatch).where(ArenaMatch.id == match_id))
    ).scalar_one()


async def _get_profile(session, user_id: int) -> ArenaProfile | None:
    return (
        await session.execute(select(ArenaProfile).where(ArenaProfile.user_id == user_id))
    ).scalar_one_or_none()


async def _get_progress(session, profile_id: int, fighter_type: str) -> ArenaFighterProgress | None:
    return (
        await session.execute(
            select(ArenaFighterProgress).where(
                ArenaFighterProgress.profile_id == profile_id,
                ArenaFighterProgress.fighter_type == fighter_type,
            )
        )
    ).scalar_one_or_none()


async def _cleanup_real_commit_rows(user_ids: list[int]) -> None:
    """Deletes every row the small handful of tests below that do REAL
    commits (not the rolled-back `session` fixture) may have created for
    `user_ids`, in FK-safe order — adversarial-review finding: an earlier
    version of `test_runtime_worker_settles_terminal_match_via_scheduled_job`
    left permanent rows in the shared sandbox Postgres on every run (a
    `users.id` literal collision with an unrelated test elsewhere in the
    repo was hit and manually cleaned up once during development; this
    makes that cleanup automatic instead of relying on it never recurring)."""
    from sqlalchemy import delete

    from common.db.session import SessionLocal
    from common.models.economy_tx import EconomyTx

    async with SessionLocal() as cleanup_session:
        await cleanup_session.execute(
            delete(ArenaFighterProgress).where(
                ArenaFighterProgress.profile_id.in_(
                    select(ArenaProfile.id).where(ArenaProfile.user_id.in_(user_ids))
                )
            )
        )
        await cleanup_session.execute(
            delete(ArenaMatch).where(
                ArenaMatch.creator_id.in_(user_ids) | ArenaMatch.opponent_id.in_(user_ids)
            )
        )
        await cleanup_session.execute(delete(ArenaProfile).where(ArenaProfile.user_id.in_(user_ids)))
        await cleanup_session.execute(delete(EconomyTx).where(EconomyTx.user_id.in_(user_ids)))
        await cleanup_session.execute(delete(UserBalance).where(UserBalance.user_id.in_(user_ids)))
        await cleanup_session.execute(delete(User).where(User.id.in_(user_ids)))
        await cleanup_session.commit()


async def _create_active_match(
    session,
    chat_id: int,
    creator_id: int,
    opponent_id: int,
    *,
    creator_bet: int = 100,
    opponent_bet: int = 100,
    creator_fighter: str = FighterType.TANK.value,
    opponent_fighter: str = FighterType.ASSASSIN.value,
    idempotency_key: str,
) -> ArenaMatch:
    """Drives a match through the real lobby lifecycle (create/accept/confirm
    both sides) up to `status="active"` — the exact precondition
    `settle_match` requires, exercising the real escrow path rather than
    hand-inserting a row that skips it."""
    match = await arena_service.create_match(
        session, chat_id, creator_id, creator_fighter, creator_bet, f"{idempotency_key}:create"
    )
    match = await arena_service.accept_match(
        session,
        chat_id,
        match.id,
        opponent_id,
        opponent_fighter,
        opponent_bet,
        f"{idempotency_key}:accept",
    )
    await arena_service.confirm_match(session, chat_id, match.id, creator_id)
    match = await arena_service.confirm_match(session, chat_id, match.id, opponent_id)
    assert match.status == "active"
    return match


def _pot_fee_payout(creator_bet: int, opponent_bet: int) -> tuple[int, int, int, int]:
    """Mirrors `arena_service.settle_match`'s exact fee math — plain
    `round()` uses Python's banker's rounding (round-half-to-even) on a
    float, which diverges from production's `Decimal`+`ROUND_HALF_UP` at
    exact `.5` boundaries (e.g. pot=210 -> fee 10 vs production's 11).
    Adversarial-review finding: this helper must replicate the real formula
    exactly, not just "close enough", or a boundary-case test would assert
    the wrong expected value."""
    pot = creator_bet + opponent_bet
    fee_total = int(
        (Decimal(pot) * _CONFIG.fee_percent / Decimal(100)).quantize(
            Decimal("1"), rounding=ROUND_HALF_UP
        )
    )
    fee_fund = fee_total // 2
    fee_chat = fee_total - fee_fund
    payout = pot - fee_total
    return pot, fee_chat, fee_fund, payout


# --- settle_match: decisive win ----------------------------------------------


@pytest.mark.asyncio
async def test_settle_win_pays_winner_pot_minus_fee_split(session):
    chat_id = -100930001
    creator_id, opponent_id = 930001, 930002
    await _ensure_user(session, creator_id)
    await _ensure_user(session, opponent_id)
    creator_before = await _fund(session, chat_id, creator_id)
    opponent_before = await _fund(session, chat_id, opponent_id)
    bank_before = await _get_bank_balance(session, chat_id)
    fund_before = await _get_arena_fund_balance(session, chat_id)

    match = await _create_active_match(
        session, chat_id, creator_id, opponent_id, idempotency_key="win1"
    )

    _, fee_chat, fee_fund, payout = _pot_fee_payout(100, 100)
    state = {
        "terminal": True,
        "result": "win",
        "winner_id": creator_id,
        "loser_id": opponent_id,
        "reason": "knockout",
        "refund_required": False,
    }
    settled = await arena_service.settle_match(session, chat_id, match.id, state)

    assert settled.status == "finished"
    assert settled.settlement_status == "paid"
    assert settled.match_result == "win"
    assert settled.winner_id == creator_id
    assert settled.loser_id == opponent_id
    assert settled.payout_amount == payout
    assert settled.fee_chat == fee_chat
    assert settled.fee_fund == fee_fund
    assert payout + fee_chat + fee_fund == 200  # invariant: nothing created/destroyed

    assert await _get_user_balance(session, chat_id, creator_id) == creator_before - 100 + payout
    assert await _get_user_balance(session, chat_id, opponent_id) == opponent_before - 100
    assert await _get_bank_balance(session, chat_id) == bank_before + fee_chat
    assert await _get_arena_fund_balance(session, chat_id) == fund_before + fee_fund


@pytest.mark.asyncio
async def test_settle_win_unequal_stakes(session):
    chat_id = -100930003
    creator_id, opponent_id = 930003, 930004
    await _ensure_user(session, creator_id)
    await _ensure_user(session, opponent_id)
    creator_before = await _fund(session, chat_id, creator_id)
    opponent_before = await _fund(session, chat_id, opponent_id)

    match = await _create_active_match(
        session,
        chat_id,
        creator_id,
        opponent_id,
        creator_bet=100,
        opponent_bet=300,
        idempotency_key="win2",
    )

    pot, fee_chat, fee_fund, payout = _pot_fee_payout(100, 300)
    assert pot == 400
    state = {
        "terminal": True,
        "result": "win",
        "winner_id": opponent_id,
        "loser_id": creator_id,
        "reason": "time_limit",
        "refund_required": False,
    }
    settled = await arena_service.settle_match(session, chat_id, match.id, state)

    assert settled.payout_amount == payout
    assert payout + fee_chat + fee_fund == pot
    # Winner (opponent) staked 300, gets back payout (> their own stake since
    # they also take the challenger's 100 minus fee).
    assert await _get_user_balance(session, chat_id, opponent_id) == opponent_before - 300 + payout
    assert await _get_user_balance(session, chat_id, creator_id) == creator_before - 100


@pytest.mark.asyncio
async def test_settle_win_fee_rounding_at_exact_half_boundary(session):
    """Adversarial-review finding: pot=210 -> 5% = 10.5 exactly, the one
    value where `Decimal(...).quantize(..., ROUND_HALF_UP)` (production) and
    Python's plain `round()` (banker's rounding, round-half-to-even) give
    DIFFERENT answers (11 vs 10) — pins production's actual behavior at that
    boundary rather than trusting a test helper that could silently drift."""
    chat_id = -100930021
    creator_id, opponent_id = 930023, 930024
    await _ensure_user(session, creator_id)
    await _ensure_user(session, opponent_id)
    creator_before = await _fund(session, chat_id, creator_id)
    opponent_before = await _fund(session, chat_id, opponent_id)

    match = await _create_active_match(
        session,
        chat_id,
        creator_id,
        opponent_id,
        creator_bet=110,
        opponent_bet=100,
        idempotency_key="feeround1",
    )
    state = {
        "terminal": True,
        "result": "win",
        "winner_id": creator_id,
        "loser_id": opponent_id,
        "reason": "knockout",
        "refund_required": False,
    }
    settled = await arena_service.settle_match(session, chat_id, match.id, state)

    # pot=210, 5% = 10.5 -> ROUND_HALF_UP -> 11 (not 10, which plain round()
    # would give). fee_fund=5, fee_chat=6, payout=199.
    assert settled.payout_amount == 199
    assert settled.fee_chat == 6
    assert settled.fee_fund == 5
    assert settled.payout_amount + settled.fee_chat + settled.fee_fund == 210
    assert await _get_user_balance(session, chat_id, creator_id) == creator_before - 110 + 199
    assert await _get_user_balance(session, chat_id, opponent_id) == opponent_before - 100


@pytest.mark.asyncio
async def test_settle_technical_loss_pays_same_as_win_but_worse_loser_penalty(session):
    chat_id = -100930005
    creator_id, opponent_id = 930005, 930006
    await _ensure_user(session, creator_id)
    await _ensure_user(session, opponent_id)
    await _fund(session, chat_id, creator_id)
    await _fund(session, chat_id, opponent_id)

    match = await _create_active_match(
        session, chat_id, creator_id, opponent_id, idempotency_key="techloss1"
    )
    state = {
        "terminal": True,
        "result": "technical_loss",
        "winner_id": creator_id,
        "loser_id": opponent_id,
        "reason": "disconnect_timeout",
        "refund_required": False,
    }
    settled = await arena_service.settle_match(session, chat_id, match.id, state)

    _, _, _, payout = _pot_fee_payout(100, 100)
    assert settled.settlement_status == "paid"
    assert settled.payout_amount == payout

    winner_profile = await _get_profile(session, creator_id)
    loser_profile = await _get_profile(session, opponent_id)
    assert winner_profile.rating == _CONFIG.initial_rating + _CONFIG.win_rating_delta
    assert winner_profile.technical_wins == 1
    assert loser_profile.rating == _CONFIG.initial_rating - _CONFIG.technical_loss_rating_delta
    assert loser_profile.technical_losses == 1

    winner_progress = await _get_progress(session, winner_profile.id, match.creator_fighter)
    loser_progress = await _get_progress(session, loser_profile.id, match.opponent_fighter)
    assert winner_progress.xp == _CONFIG.base_match_xp + _CONFIG.win_bonus_xp
    assert loser_progress is None  # 0 XP on technical loss -> no progress row created


# --- settle_match: draw -------------------------------------------------------


@pytest.mark.asyncio
async def test_settle_draw_refunds_both_no_fee_but_grants_xp(session):
    chat_id = -100930007
    creator_id, opponent_id = 930007, 930008
    await _ensure_user(session, creator_id)
    await _ensure_user(session, opponent_id)
    creator_before = await _fund(session, chat_id, creator_id)
    opponent_before = await _fund(session, chat_id, opponent_id)
    bank_before = await _get_bank_balance(session, chat_id)
    fund_before = await _get_arena_fund_balance(session, chat_id)

    match = await _create_active_match(
        session, chat_id, creator_id, opponent_id, idempotency_key="draw1"
    )
    state = {
        "terminal": True,
        "result": "draw",
        "winner_id": None,
        "loser_id": None,
        "reason": "time_limit",
        "refund_required": False,
    }
    settled = await arena_service.settle_match(session, chat_id, match.id, state)

    assert settled.status == "finished"
    assert settled.settlement_status == "refunded"
    assert settled.match_result == "draw"
    assert settled.payout_amount is None

    # Full refund, no fee taken (FIGHTING_SPEC.md §6.2).
    assert await _get_user_balance(session, chat_id, creator_id) == creator_before
    assert await _get_user_balance(session, chat_id, opponent_id) == opponent_before
    assert await _get_bank_balance(session, chat_id) == bank_before
    assert await _get_arena_fund_balance(session, chat_id) == fund_before

    # But XP is still granted to both sides (spec §8.2: "PvP-ничья: 100 XP"),
    # rating stays untouched.
    creator_profile = await _get_profile(session, creator_id)
    opponent_profile = await _get_profile(session, opponent_id)
    assert creator_profile.rating == _CONFIG.initial_rating
    assert creator_profile.draws == 1
    assert opponent_profile.draws == 1
    creator_progress = await _get_progress(session, creator_profile.id, match.creator_fighter)
    opponent_progress = await _get_progress(session, opponent_profile.id, match.opponent_fighter)
    assert creator_progress.xp == _CONFIG.base_match_xp
    assert opponent_progress.xp == _CONFIG.base_match_xp


# --- settle_match: both disconnected (pure refund, no stats) ----------------


@pytest.mark.asyncio
async def test_settle_both_disconnected_refunds_and_records_no_stats(session):
    chat_id = -100930009
    creator_id, opponent_id = 930009, 930010
    await _ensure_user(session, creator_id)
    await _ensure_user(session, opponent_id)
    creator_before = await _fund(session, chat_id, creator_id)
    opponent_before = await _fund(session, chat_id, opponent_id)

    match = await _create_active_match(
        session, chat_id, creator_id, opponent_id, idempotency_key="bothdc1"
    )
    state = {
        "terminal": True,
        "result": None,
        "winner_id": None,
        "loser_id": None,
        "reason": "both_disconnected",
        "refund_required": True,
    }
    settled = await arena_service.settle_match(session, chat_id, match.id, state)

    assert settled.status == "finished"
    assert settled.settlement_status == "refunded"
    assert settled.match_result is None
    assert await _get_user_balance(session, chat_id, creator_id) == creator_before
    assert await _get_user_balance(session, chat_id, opponent_id) == opponent_before

    # No rating/XP consequence for a match that never really happened.
    assert await _get_profile(session, creator_id) is None
    assert await _get_profile(session, opponent_id) is None


# --- Идемпотентность / money-safety regressions ------------------------------


@pytest.mark.asyncio
async def test_settle_idempotent_on_repeat_call(session):
    chat_id = -100930011
    creator_id, opponent_id = 930011, 930012
    await _ensure_user(session, creator_id)
    await _ensure_user(session, opponent_id)
    await _fund(session, chat_id, creator_id)
    await _fund(session, chat_id, opponent_id)

    match = await _create_active_match(
        session, chat_id, creator_id, opponent_id, idempotency_key="idem1"
    )
    state = {
        "terminal": True,
        "result": "win",
        "winner_id": creator_id,
        "loser_id": opponent_id,
        "reason": "knockout",
        "refund_required": False,
    }
    first = await arena_service.settle_match(session, chat_id, match.id, state)
    balance_after_first = await _get_user_balance(session, chat_id, creator_id)
    winner_profile_after_first = await _get_profile(session, creator_id)
    loser_profile_after_first = await _get_profile(session, opponent_id)
    winner_rating_after_first = winner_profile_after_first.rating
    winner_wins_after_first = winner_profile_after_first.wins
    loser_rating_after_first = loser_profile_after_first.rating
    winner_progress_after_first = await _get_progress(
        session, winner_profile_after_first.id, match.creator_fighter
    )
    winner_xp_after_first = winner_progress_after_first.xp
    winner_level_after_first = winner_progress_after_first.level

    second = await arena_service.settle_match(session, chat_id, match.id, state)

    assert second.settlement_status == first.settlement_status == "paid"
    assert second.payout_amount == first.payout_amount
    assert await _get_user_balance(session, chat_id, creator_id) == balance_after_first

    # Roadmap Phase 5 "Проверка" list explicitly calls out rating/XP
    # idempotency alongside the money check — a repeat settle must not
    # double-apply rating deltas or XP grants either.
    winner_profile_after_second = await _get_profile(session, creator_id)
    loser_profile_after_second = await _get_profile(session, opponent_id)
    assert winner_profile_after_second.rating == winner_rating_after_first
    assert winner_profile_after_second.wins == winner_wins_after_first
    assert loser_profile_after_second.rating == loser_rating_after_first
    winner_progress_after_second = await _get_progress(
        session, winner_profile_after_second.id, match.creator_fighter
    )
    assert winner_progress_after_second.xp == winner_xp_after_first
    assert winner_progress_after_second.level == winner_level_after_first


@pytest.mark.asyncio
async def test_settle_non_active_or_already_settled_is_a_noop(session):
    chat_id = -100930013
    creator_id, opponent_id = 930013, 930014
    await _ensure_user(session, creator_id)
    await _ensure_user(session, opponent_id)
    await _fund(session, chat_id, creator_id)
    await _fund(session, chat_id, opponent_id)

    # Match still "waiting" (never accepted) -> settle_match must not touch it.
    match = await arena_service.create_match(
        session, chat_id, creator_id, FighterType.TANK, 100, "noop1:create"
    )
    settled = await arena_service.settle_match(
        session, chat_id, match.id, {"terminal": True, "result": "win", "winner_id": creator_id}
    )
    assert settled.status == "waiting"
    assert settled.settlement_status == "pending"


@pytest.mark.asyncio
async def test_settle_payout_failure_after_winner_credit_does_not_leave_partial_state(
    session, monkeypatch
):
    """Same regression class (and same test-scope caveat) as
    test_duel_service.py::test_accept_duel_payout_failure_after_rng_does_not_fabricate_or_double_charge
    — forces `credit_bank` (the SECOND money-moving call, right after the
    winner's `credit()` already succeeded within the same uncommitted
    transaction) to raise. The exception must propagate unmodified (not get
    swallowed into a half-applied match row), and the match must NOT be
    recorded as settled: `settle_match` (like `accept_duel`/
    `accept_autobattle`) does no internal rollback, so we verify the money
    invariant WITHOUT an explicit `session.rollback()` here — the escrow
    from `_create_active_match` (already committed earlier) plus the
    winner's payout `credit()` (applied but not yet committed in THIS call)
    are both still visible within the current transaction; only the
    fee_chat/match-row writes AFTER the simulated failure never happened."""
    chat_id = -100930015
    creator_id, opponent_id = 930015, 930016
    await _ensure_user(session, creator_id)
    await _ensure_user(session, opponent_id)
    creator_before = await _fund(session, chat_id, creator_id)
    opponent_before = await _fund(session, chat_id, opponent_id)

    match = await _create_active_match(
        session, chat_id, creator_id, opponent_id, idempotency_key="rollback1"
    )

    monkeypatch.setattr(
        economy_service,
        "credit_bank",
        AsyncMock(side_effect=RuntimeError("simulated DB failure after winner payout")),
    )
    state = {
        "terminal": True,
        "result": "win",
        "winner_id": creator_id,
        "loser_id": opponent_id,
        "reason": "knockout",
        "refund_required": False,
    }
    with pytest.raises(RuntimeError):
        await arena_service.settle_match(session, chat_id, match.id, state)

    # The match row was never committed as settled — status/settlement_status
    # writes happen AFTER the fee calls, which never completed.
    match_row = await _get_match(session, match.id)
    assert match_row.status == "active"
    assert match_row.settlement_status == "pending"
    assert match_row.payout_amount is None
    # Escrow (from match creation/acceptance, already committed) is real;
    # the winner's payout credit() was applied in-transaction but never
    # committed — a real SessionLocal() caller would discard it on close.
    _, _, _, payout = _pot_fee_payout(100, 100)
    assert await _get_user_balance(session, chat_id, creator_id) == creator_before - 100 + payout
    assert await _get_user_balance(session, chat_id, opponent_id) == opponent_before - 100


# --- XP threshold / leveling --------------------------------------------------


def test_xp_threshold_matches_spec_formula():
    assert arena_service._xp_threshold(1) == 500
    assert arena_service._xp_threshold(2) == 600
    assert arena_service._xp_threshold(10) == 1400


@pytest.mark.asyncio
async def test_settle_win_levels_up_fighter_when_xp_crosses_threshold(session):
    chat_id = -100930017
    creator_id, opponent_id = 930017, 930018
    await _ensure_user(session, creator_id)
    await _ensure_user(session, opponent_id)
    await _fund(session, chat_id, creator_id)
    await _fund(session, chat_id, opponent_id)

    # Pre-seed the winner's fighter progress just 10 XP below level 1's
    # threshold (500) so this single win's +150 XP crosses it with room to
    # spare, exercising the level-up loop (not just accumulation).
    match = await _create_active_match(
        session, chat_id, creator_id, opponent_id, idempotency_key="level1"
    )
    profile = await arena_service._get_or_create_profile_for_update(session, creator_id)
    progress = await arena_service._get_or_create_fighter_progress_for_update(
        session, profile.id, match.creator_fighter
    )
    progress.xp = 490
    await session.commit()

    state = {
        "terminal": True,
        "result": "win",
        "winner_id": creator_id,
        "loser_id": opponent_id,
        "reason": "knockout",
        "refund_required": False,
    }
    await arena_service.settle_match(session, chat_id, match.id, state)

    updated = await _get_progress(session, profile.id, match.creator_fighter)
    # 490 + 150 = 640; threshold(1) = 500 -> level 2, remaining xp = 140.
    assert updated.level == 2
    assert updated.xp == 140


# --- Runtime worker wiring: real ArenaSessionService terminal state ---------


class _Lock:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None


class _FakeRedis:
    """Same minimal in-memory stub as tests/arena/test_session_service.py's
    FakeRedis — reused here so the worker-wiring test drives a REAL
    ArenaSessionService to a genuine terminal snapshot (not a hand-crafted
    dict), proving settle_match's assumptions about the state shape actually
    hold against the real producer."""

    def __init__(self):
        self.data: dict[str, str] = {}

    async def get(self, key: str):
        return self.data.get(key)

    async def set(self, key: str, value: str, **kwargs):
        self.data[key] = value
        return True

    async def publish(self, channel: str, payload: str):
        return 1

    def lock(self, key: str, **kwargs):
        return _Lock()

    async def aclose(self):
        return None


@pytest.mark.asyncio
async def test_settle_after_real_forfeit_pays_winner(session):
    """End-to-end: a genuine ArenaSessionService.forfeit() terminal snapshot
    (not a hand-crafted dict) fed straight into settle_match — proves the two
    modules' contracts actually match, not just what this test file assumes
    that contract is."""
    from bot.services.arena_session_service import ArenaSessionService

    chat_id = -100930019
    creator_id, opponent_id = 930019, 930020
    await _ensure_user(session, creator_id)
    await _ensure_user(session, opponent_id)
    creator_before = await _fund(session, chat_id, creator_id)
    opponent_before = await _fund(session, chat_id, opponent_id)

    match = await _create_active_match(
        session, chat_id, creator_id, opponent_id, idempotency_key="forfeit1"
    )

    redis = _FakeRedis()
    runtime = ArenaSessionService(redis)
    await runtime.start_or_get(match)
    state = await runtime.forfeit(match, creator_id)  # creator forfeits -> opponent wins
    assert state["terminal"] is True
    assert state["result"] == "technical_loss"
    assert state["winner_id"] == opponent_id
    assert state["loser_id"] == creator_id

    settled = await arena_service.settle_match(session, chat_id, match.id, state)

    _, _, _, payout = _pot_fee_payout(100, 100)
    assert settled.settlement_status == "paid"
    assert settled.winner_id == opponent_id
    assert await _get_user_balance(session, chat_id, opponent_id) == opponent_before - 100 + payout
    assert await _get_user_balance(session, chat_id, creator_id) == creator_before - 100


@pytest.mark.asyncio
async def test_settle_concurrent_calls_pay_exactly_once():
    """Adversarial-review finding: the suite only tested idempotency via two
    SEQUENTIAL calls sharing one session/transaction, which never actually
    exercises the `FOR UPDATE` row lock's serialization — two callers on the
    SAME session can't race each other. This drives two GENUINELY concurrent
    `settle_match` calls from two separate `SessionLocal()` connections
    (`asyncio.gather`) against the same already-terminal match, mirroring
    the realistic race: two overlapping runtime-tick iterations (or a tick
    racing a client's `submit_action`/`forfeit` call) both observing
    `state["terminal"]` before either has settled the row yet. Exactly one
    payout must land; the loser must never see a double debit either."""
    import asyncio
    import secrets
    import uuid

    from bot.services.arena_session_service import ArenaSessionService
    from common.db.session import engine
    from common.db.session import SessionLocal

    await engine.dispose()

    chat_id = -(900_000_000 + secrets.randbelow(90_000_000))
    creator_id = 900_000_000 + secrets.randbelow(90_000_000)
    opponent_id = 900_000_000 + secrets.randbelow(90_000_000)
    try:
        async with SessionLocal() as setup_session:
            await _ensure_user(setup_session, creator_id)
            await _ensure_user(setup_session, opponent_id)
            creator_before = await _fund(setup_session, chat_id, creator_id)
            opponent_before = await _fund(setup_session, chat_id, opponent_id)
            match = await _create_active_match(
                setup_session, chat_id, creator_id, opponent_id, idempotency_key=str(uuid.uuid4())
            )

        fake_redis = _FakeRedis()
        runtime = ArenaSessionService(fake_redis)
        await runtime.start_or_get(match)
        state = await runtime.forfeit(match, creator_id)

        async with SessionLocal() as session_a, SessionLocal() as session_b:
            results = await asyncio.gather(
                arena_service.settle_match(session_a, chat_id, match.id, state),
                arena_service.settle_match(session_b, chat_id, match.id, state),
            )

        assert {r.settlement_status for r in results} == {"paid"}
        assert results[0].payout_amount == results[1].payout_amount

        _, _, _, payout = _pot_fee_payout(100, 100)
        async with SessionLocal() as check_session:
            assert (
                await _get_user_balance(check_session, chat_id, opponent_id)
                == opponent_before - 100 + payout
            )
            assert (
                await _get_user_balance(check_session, chat_id, creator_id)
                == creator_before - 100
            )
    finally:
        await _cleanup_real_commit_rows([creator_id, opponent_id])
        await engine.dispose()


@pytest.mark.asyncio
async def test_runtime_worker_settles_terminal_match_via_scheduled_job(monkeypatch):
    """Drives the ACTUAL scheduled job function (captured via the same
    `_Scheduler` double as test_scheduler_runtime.py) end-to-end: a match
    already terminal in Redis (via a real forfeit()) gets picked up by one
    worker tick and settled in Postgres — proves the wiring added to
    arena_runtime_worker.py's `_job()`, not just settle_match in isolation.

    Deliberately does NOT use the shared `session` fixture: the worker opens
    its own `SessionLocal()` connections (as it does in production), which
    can't see uncommitted rows from the fixture's wrapped, never-committed
    test transaction. Matches tests/test_api_duel.py's established
    convention for testing code that manages its own session lifecycle —
    real `SessionLocal()` + real commits, visible across connections. Cleans
    up its own rows in a `finally` (adversarial-review finding: an earlier
    version of this test leaked permanent rows into the shared sandbox
    Postgres on every run)."""
    import secrets
    import uuid
    import redis.asyncio as redis_module

    from bot.services import arena_runtime_worker
    from bot.services.arena_session_service import ArenaSessionService
    from common.db.session import engine
    from common.db.session import SessionLocal

    await engine.dispose()

    # Randomized, not literal, IDs: this is one of the tests in the suite
    # that does REAL commits against the shared sandbox Postgres (see
    # docstring) rather than the fixture's rolled-back transaction, so a
    # hardcoded literal here risks permanently colliding with an unrelated
    # test elsewhere in the repo that happens to pick the same number.
    chat_id = -(900_000_000 + secrets.randbelow(90_000_000))
    creator_id = 900_000_000 + secrets.randbelow(90_000_000)
    opponent_id = 900_000_000 + secrets.randbelow(90_000_000)
    try:
        async with SessionLocal() as setup_session:
            await _ensure_user(setup_session, creator_id)
            await _ensure_user(setup_session, opponent_id)
            await _fund(setup_session, chat_id, creator_id)
            await _fund(setup_session, chat_id, opponent_id)
            # Real commits against the long-lived sandbox Postgres (see
            # class docstring) — a fresh idempotency key per run avoids
            # colliding with a prior run's already-consumed ref_id
            # (DuplicateRequest).
            match = await _create_active_match(
                setup_session, chat_id, creator_id, opponent_id, idempotency_key=str(uuid.uuid4())
            )

        fake_redis = _FakeRedis()
        runtime = ArenaSessionService(fake_redis)
        await runtime.start_or_get(match)
        await runtime.forfeit(match, creator_id)

        monkeypatch.setattr(arena_runtime_worker, "_runtime_client", None)
        monkeypatch.setattr(redis_module, "from_url", lambda *a, **k: fake_redis)

        class _Scheduler:
            def __init__(self):
                self.jobs = []

            def add_job(self, func, trigger, **kwargs):
                self.jobs.append(func)

        scheduler = _Scheduler()
        arena_runtime_worker.register_runtime_tick(scheduler)
        job = scheduler.jobs[0]

        await job()

        async with SessionLocal() as check_session:
            settled = await _get_match(check_session, match.id)
            assert settled.status == "finished"
            assert settled.settlement_status == "paid"
            assert settled.winner_id == opponent_id
    finally:
        await _cleanup_real_commit_rows([creator_id, opponent_id])
        await engine.dispose()
