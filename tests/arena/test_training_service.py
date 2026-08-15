"""Tests for Arena training mode (`bot/services/arena_training_service.py`,
`common/arena/training_ai.py`) — Phase 6, FIGHTING_SPEC.md §9. Pure/mocked
Redis, same style as test_session_service.py (this module has zero
Postgres/economy_service involvement by design — see the service's own
module docstring — so there's nothing here that needs a real DB, unlike
the settlement tests)."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from bot.services.arena_training_service import ArenaTrainingService
from bot.services.arena_training_service import TrainingActionNotAvailable
from bot.services.arena_training_service import TrainingSessionNotFound
from bot.services.arena_training_service import TrainingSessionPaused
from common.arena.config import ArenaConfig
from common.arena.engine import CombatantState
from common.arena.fighters import FIGHTERS
from common.arena.schemas import ArenaPlayerAction
from common.arena.schemas import FighterType
from common.arena.training_ai import pick_ai_action


class _Lock:
    """Same no-op async-context-manager lock stub as
    test_session_service.py's `_Lock` — `ArenaTrainingService` now takes the
    same `redis.lock(...)` out on every mutating call that
    `ArenaSessionService` does (adversarial-review finding: unlocked
    read-modify-write could drop an action under concurrent requests)."""

    async def acquire(self, *args, **kwargs):
        return True

    async def release(self):
        return None

    async def __aenter__(self):
        await self.acquire()
        return self

    async def __aexit__(self, *args):
        await self.release()


class FakeRedis:
    """Same minimal in-memory stub as test_session_service.py's FakeRedis."""

    def __init__(self):
        self.data: dict[str, str] = {}

    async def get(self, key: str):
        return self.data.get(key)

    async def set(self, key: str, value: str, **kwargs):
        self.data[key] = value
        return True

    def lock(self, key: str, **kwargs):
        return _Lock()


class _FixedChoiceRng:
    """Forces `.choice(seq)` to always return the same element (or, for the
    AI-picker tests, a specific member of the passed sequence) — deterministic
    substitute for `secrets.SystemRandom`."""

    def __init__(self, value):
        self._value = value

    def choice(self, seq):
        return self._value if self._value in seq else seq[0]


@pytest.fixture
def service():
    return ArenaTrainingService(FakeRedis())


CHAT_ID = -800001
USER_ID = 501


# --- training_ai.pick_ai_action ------------------------------------------


def test_pick_ai_action_excludes_dodge_on_cooldown():
    combatant = CombatantState(fighter=FIGHTERS[FighterType.TANK], hp=100, dodge_cooldown_remaining=2)
    config = ArenaConfig()
    rng = _FixedChoiceRng(ArenaPlayerAction.DODGE)
    action = pick_ai_action(combatant, config, rng)
    assert action != ArenaPlayerAction.DODGE


def test_pick_ai_action_excludes_special_without_energy():
    combatant = CombatantState(fighter=FIGHTERS[FighterType.TANK], hp=100, energy=0)
    config = ArenaConfig()
    rng = _FixedChoiceRng(ArenaPlayerAction.SPECIAL)
    action = pick_ai_action(combatant, config, rng)
    assert action != ArenaPlayerAction.SPECIAL


def test_pick_ai_action_allows_special_at_full_energy():
    config = ArenaConfig()
    combatant = CombatantState(
        fighter=FIGHTERS[FighterType.TANK], hp=100, energy=config.special_energy_cost
    )
    rng = _FixedChoiceRng(ArenaPlayerAction.SPECIAL)
    action = pick_ai_action(combatant, config, rng)
    assert action == ArenaPlayerAction.SPECIAL


def test_pick_ai_action_never_returns_empty_choice_set():
    """Only DODGE/SPECIAL can ever be filtered — FAST_ATTACK/HEAVY_ATTACK/
    BLOCK are always legal, so the available set is never empty even when
    both are excluded simultaneously."""
    config = ArenaConfig()
    combatant = CombatantState(
        fighter=FIGHTERS[FighterType.TANK], hp=100, energy=0, dodge_cooldown_remaining=1
    )

    class _RecordingRng:
        def choice(self, seq):
            assert len(seq) > 0
            assert ArenaPlayerAction.DODGE not in seq
            assert ArenaPlayerAction.SPECIAL not in seq
            return seq[0]

    pick_ai_action(combatant, config, _RecordingRng())


# --- ArenaTrainingService.start_or_restart --------------------------------


@pytest.mark.asyncio
async def test_start_creates_fresh_session_with_chosen_fighter(service):
    state = await service.start_or_restart(CHAT_ID, USER_ID, FighterType.TANK, FighterType.ASSASSIN)

    assert state["fighters"] == {"player": "tank", "opponent": "assassin"}
    assert state["phase_index"] == 0
    assert state["terminal"] is False
    assert state["paused"] is False
    assert state["engine"]["player"]["hp"] == FIGHTERS[FighterType.TANK].max_hp
    assert state["engine"]["opponent"]["hp"] == FIGHTERS[FighterType.ASSASSIN].max_hp


@pytest.mark.asyncio
async def test_start_picks_random_opponent_when_not_specified(service, monkeypatch):
    monkeypatch.setattr(service, "_rng", _FixedChoiceRng(FighterType.BERSERKER))
    state = await service.start_or_restart(CHAT_ID, USER_ID, FighterType.TANK)
    assert state["fighters"]["opponent"] == "berserker"


@pytest.mark.asyncio
async def test_restart_clears_old_state(service, monkeypatch):
    monkeypatch.setattr(service, "_rng", _FixedChoiceRng(ArenaPlayerAction.BLOCK))
    first = await service.start_or_restart(CHAT_ID, USER_ID, FighterType.TANK, FighterType.ASSASSIN)
    await service.submit_action(CHAT_ID, USER_ID, ArenaPlayerAction.FAST_ATTACK)
    mid_state = await service.get_state(CHAT_ID, USER_ID)
    assert mid_state["phase_index"] == 1
    assert mid_state["last_outcome"] is not None

    restarted = await service.start_or_restart(
        CHAT_ID, USER_ID, FighterType.BERSERKER, FighterType.TACTICIAN
    )
    assert restarted["phase_index"] == 0
    assert restarted["fighters"] == {"player": "berserker", "opponent": "tactician"}
    assert first["fighters"] != restarted["fighters"]

    # Every field a mid-fight session could have set is reset, not just
    # phase_index/fighters (adversarial-review coverage gap).
    assert restarted["paused"] is False
    assert restarted["terminal"] is False
    assert restarted["result"] is None
    assert restarted["winner"] is None
    assert restarted["last_outcome"] is None
    assert restarted["engine"]["player"]["hp"] == FIGHTERS[FighterType.BERSERKER].max_hp
    assert restarted["engine"]["opponent"]["hp"] == FIGHTERS[FighterType.TACTICIAN].max_hp


@pytest.mark.asyncio
async def test_start_rejects_unknown_fighter(service):
    from bot.services.arena_training_service import ArenaTrainingError

    with pytest.raises(ArenaTrainingError):
        await service.start_or_restart(CHAT_ID, USER_ID, "not_a_real_fighter")


# --- get_state / not-found -------------------------------------------------


@pytest.mark.asyncio
async def test_get_state_without_session_raises_not_found(service):
    with pytest.raises(TrainingSessionNotFound):
        await service.get_state(CHAT_ID, USER_ID)


# --- submit_action -----------------------------------------------------------


@pytest.mark.asyncio
async def test_submit_action_resolves_one_phase_synchronously(service, monkeypatch):
    monkeypatch.setattr(service, "_rng", _FixedChoiceRng(ArenaPlayerAction.BLOCK))
    await service.start_or_restart(CHAT_ID, USER_ID, FighterType.TANK, FighterType.ASSASSIN)

    state = await service.submit_action(CHAT_ID, USER_ID, ArenaPlayerAction.FAST_ATTACK)

    assert state["phase_index"] == 1
    assert state["last_outcome"]["player_action"] == "fast_attack"
    assert state["last_outcome"]["opponent_action"] == "block"


@pytest.mark.asyncio
async def test_submit_action_on_paused_session_raises(service):
    await service.start_or_restart(CHAT_ID, USER_ID, FighterType.TANK, FighterType.ASSASSIN)
    await service.set_paused(CHAT_ID, USER_ID, True)

    with pytest.raises(TrainingSessionPaused):
        await service.submit_action(CHAT_ID, USER_ID, ArenaPlayerAction.FAST_ATTACK)


@pytest.mark.asyncio
async def test_submit_action_invalid_action_string_raises(service):
    await service.start_or_restart(CHAT_ID, USER_ID, FighterType.TANK, FighterType.ASSASSIN)
    with pytest.raises(TrainingActionNotAvailable):
        await service.submit_action(CHAT_ID, USER_ID, "not_a_real_action")


@pytest.mark.asyncio
async def test_submit_action_dodge_on_cooldown_raises(service, monkeypatch):
    monkeypatch.setattr(service, "_rng", _FixedChoiceRng(ArenaPlayerAction.BLOCK))
    await service.start_or_restart(CHAT_ID, USER_ID, FighterType.TANK, FighterType.ASSASSIN)
    await service.submit_action(CHAT_ID, USER_ID, ArenaPlayerAction.DODGE)

    # Dodge cooldown is 2 phases (ArenaConfig.dodge_cooldown_phases default)
    # -> immediately re-using it must be rejected.
    with pytest.raises(TrainingActionNotAvailable):
        await service.submit_action(CHAT_ID, USER_ID, ArenaPlayerAction.DODGE)


@pytest.mark.asyncio
async def test_failed_action_leaves_redis_state_byte_for_byte_unchanged(service, monkeypatch):
    """Adversarial-review coverage gap: previously only the raised
    exception was asserted, not the absence of a write — this confirms
    `_write` genuinely never runs on the failure path (dodge-on-cooldown),
    not just that an error is raised despite some write having happened."""
    monkeypatch.setattr(service, "_rng", _FixedChoiceRng(ArenaPlayerAction.BLOCK))
    await service.start_or_restart(CHAT_ID, USER_ID, FighterType.TANK, FighterType.ASSASSIN)
    await service.submit_action(CHAT_ID, USER_ID, ArenaPlayerAction.DODGE)

    redis_key = service.session_key(CHAT_ID, USER_ID)
    snapshot_before = service.redis.data[redis_key]

    with pytest.raises(TrainingActionNotAvailable):
        await service.submit_action(CHAT_ID, USER_ID, ArenaPlayerAction.DODGE)

    assert service.redis.data[redis_key] == snapshot_before


@pytest.mark.asyncio
async def test_concurrent_submit_action_does_not_lose_an_update(service, monkeypatch):
    """Adversarial-review finding: without the lock, two concurrent
    `submit_action` calls could both read `phase_index=N` and the second
    Redis SET would silently clobber the first, dropping one phase's
    outcome. `FakeRedis`/`_Lock` here don't model real contention (Python
    asyncio's cooperative scheduling means `_Lock.__aenter__` never
    actually suspends), so this mainly proves the code PATH acquires and
    releases the lock around the full read-modify-write on every call
    (confirmed by both calls succeeding and phase_index advancing
    monotonically by exactly 1 each) — the real serialization guarantee
    against genuine OS-thread/process-level concurrency comes from Redis's
    own distributed lock semantics in production, exercised for the PvP
    session service's identical pattern already."""
    import asyncio

    monkeypatch.setattr(service, "_rng", _FixedChoiceRng(ArenaPlayerAction.BLOCK))
    await service.start_or_restart(CHAT_ID, USER_ID, FighterType.TANK, FighterType.ASSASSIN)

    results = await asyncio.gather(
        service.submit_action(CHAT_ID, USER_ID, ArenaPlayerAction.FAST_ATTACK),
        service.submit_action(CHAT_ID, USER_ID, ArenaPlayerAction.BLOCK),
    )
    phase_indices = sorted(r["phase_index"] for r in results)
    assert phase_indices == [1, 2]

    final = await service.get_state(CHAT_ID, USER_ID)
    assert final["phase_index"] == 2


@pytest.mark.asyncio
async def test_submit_action_special_without_energy_raises(service, monkeypatch):
    monkeypatch.setattr(service, "_rng", _FixedChoiceRng(ArenaPlayerAction.BLOCK))
    await service.start_or_restart(CHAT_ID, USER_ID, FighterType.TANK, FighterType.ASSASSIN)
    # Fresh session starts at 0 energy (below special_energy_cost=100).
    with pytest.raises(TrainingActionNotAvailable):
        await service.submit_action(CHAT_ID, USER_ID, ArenaPlayerAction.SPECIAL)


@pytest.mark.asyncio
async def test_submit_action_after_terminal_is_a_noop(service, monkeypatch):
    """Force a one-hit knockout (BERSERKER heavy_damage vs. a low-HP
    combatant is overkill in one hit for the weakest fighter) — verify a
    further submit_action on the now-terminal session just returns the
    terminal state without erroring or advancing phase_index."""
    monkeypatch.setattr(service, "_rng", _FixedChoiceRng(ArenaPlayerAction.FAST_ATTACK))
    await service.start_or_restart(CHAT_ID, USER_ID, FighterType.ASSASSIN, FighterType.TANK)

    state = None
    for _ in range(20):
        state = await service.submit_action(CHAT_ID, USER_ID, ArenaPlayerAction.HEAVY_ATTACK)
        if state["terminal"]:
            break
    assert state["terminal"] is True
    phase_at_terminal = state["phase_index"]

    again = await service.submit_action(CHAT_ID, USER_ID, ArenaPlayerAction.HEAVY_ATTACK)
    assert again["terminal"] is True
    assert again["phase_index"] == phase_at_terminal


def test_no_stakes_no_rating_no_xp_touched():
    """Money-safety-adjacent assertion: this module never imports
    economy_service or touches any DB-backed model at all — confirmed
    structurally (no such import line anywhere in this file) rather than by
    mocking something that could silently no-op. Checked against actual
    `import` lines, not the whole file's prose (the module's own docstring
    explains this design choice using these same words)."""
    import ast
    import inspect

    import bot.services.arena_training_service as module

    tree = ast.parse(inspect.getsource(module))
    imported_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported_names.update(alias.name for alias in node.names)
            if node.module:
                imported_names.add(node.module)

    forbidden = {"economy_service", "AsyncSession", "ArenaMatch", "ArenaProfile"}
    assert not (imported_names & forbidden)


# --- set_paused ---------------------------------------------------------


@pytest.mark.asyncio
async def test_set_paused_toggles_and_blocks_actions(service):
    await service.start_or_restart(CHAT_ID, USER_ID, FighterType.TANK, FighterType.ASSASSIN)
    paused = await service.set_paused(CHAT_ID, USER_ID, True)
    assert paused["paused"] is True

    resumed = await service.set_paused(CHAT_ID, USER_ID, False)
    assert resumed["paused"] is False


@pytest.mark.asyncio
async def test_set_paused_on_terminal_session_is_a_noop(service, monkeypatch):
    monkeypatch.setattr(service, "_rng", _FixedChoiceRng(ArenaPlayerAction.FAST_ATTACK))
    await service.start_or_restart(CHAT_ID, USER_ID, FighterType.ASSASSIN, FighterType.TANK)

    state = None
    for _ in range(20):
        state = await service.submit_action(CHAT_ID, USER_ID, ArenaPlayerAction.HEAVY_ATTACK)
        if state["terminal"]:
            break
    assert state["terminal"] is True

    result = await service.set_paused(CHAT_ID, USER_ID, True)
    assert result["paused"] is False  # unchanged — terminal sessions ignore pause


# --- Isolation between users -------------------------------------------------


@pytest.mark.asyncio
async def test_sessions_isolated_per_user(service):
    await service.start_or_restart(CHAT_ID, 601, FighterType.TANK, FighterType.ASSASSIN)
    await service.start_or_restart(CHAT_ID, 602, FighterType.BERSERKER, FighterType.TACTICIAN)

    state_a = await service.get_state(CHAT_ID, 601)
    state_b = await service.get_state(CHAT_ID, 602)
    assert state_a["fighters"]["player"] == "tank"
    assert state_b["fighters"]["player"] == "berserker"


@pytest.mark.asyncio
async def test_redis_client_receives_ttl_on_write():
    redis = AsyncMock()
    redis.get = AsyncMock(return_value=None)
    redis.lock = lambda *args, **kwargs: _Lock()
    service = ArenaTrainingService(redis)
    await service.start_or_restart(CHAT_ID, USER_ID, FighterType.TANK, FighterType.ASSASSIN)
    redis.set.assert_awaited_once()
    _, kwargs = redis.set.call_args
    assert kwargs.get("ex") == 3600
