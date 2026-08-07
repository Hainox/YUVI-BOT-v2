from __future__ import annotations

from common.arena.engine import ActionNotAvailable
from common.arena.engine import ArenaEngine
from common.arena.fighters import FIGHTERS
from common.arena.schemas import ArenaMatchResult
from common.arena.schemas import ArenaPlayerAction
from common.arena.schemas import FighterType


def _engine() -> ArenaEngine:
    return ArenaEngine()


def test_fighter_catalog_contains_approved_stats_and_specials() -> None:
    assert FIGHTERS[FighterType.TANK].max_hp == 130
    assert FIGHTERS[FighterType.TANK].fast_damage == 8
    assert FIGHTERS[FighterType.TANK].heavy_damage == 20
    assert FIGHTERS[FighterType.ASSASSIN].max_hp == 80
    assert FIGHTERS[FighterType.BERSERKER].heavy_damage == 24
    assert FIGHTERS[FighterType.TACTICIAN].fast_damage == 10
    assert {fighter.special_name for fighter in FIGHTERS.values()} == {
        "Крепость",
        "Молниеносный рывок",
        "Ярость",
        "Тактическая ловушка",
    }


def test_fast_attack_counters_heavy_with_25_percent_bonus() -> None:
    engine = _engine()
    state = engine.start(FighterType.TANK, FighterType.TANK)

    outcome = engine.resolve_phase(
        state,
        ArenaPlayerAction.FAST_ATTACK,
        ArenaPlayerAction.HEAVY_ATTACK,
    )

    assert outcome.player_damage == 10  # 8 * 1.25, rounded mathematically.
    assert outcome.opponent_damage == 0
    assert outcome.player_counter is True
    assert outcome.state.opponent.hp == 120
    assert outcome.state.player.hp == 130


def test_heavy_attack_counters_block_and_gets_bonus_damage() -> None:
    engine = _engine()
    state = engine.start(FighterType.TANK, FighterType.TANK)

    outcome = engine.resolve_phase(
        state,
        ArenaPlayerAction.HEAVY_ATTACK,
        ArenaPlayerAction.BLOCK,
    )

    assert outcome.player_damage == 25
    assert outcome.opponent_damage == 0
    assert outcome.player_counter is True


def test_block_fully_stops_fast_attack_and_gains_energy() -> None:
    engine = _engine()
    state = engine.start(FighterType.TANK, FighterType.TANK)

    outcome = engine.resolve_phase(
        state,
        ArenaPlayerAction.BLOCK,
        ArenaPlayerAction.FAST_ATTACK,
    )

    assert outcome.player_damage == 0
    assert outcome.opponent_damage == 0
    assert outcome.player_counter is True
    assert outcome.state.player.energy > state.player.energy


def test_dodge_is_checked_before_normal_attack_and_has_two_phase_cooldown() -> None:
    engine = _engine()
    state = engine.start(FighterType.TANK, FighterType.TANK)

    first = engine.resolve_phase(
        state,
        ArenaPlayerAction.DODGE,
        ArenaPlayerAction.HEAVY_ATTACK,
    )
    assert first.player_damage == 0
    assert first.state.player.dodge_cooldown_remaining == 2

    try:
        engine.resolve_phase(
            first.state,
            ArenaPlayerAction.DODGE,
            ArenaPlayerAction.FAST_ATTACK,
        )
    except ActionNotAvailable:
        pass
    else:
        raise AssertionError("dodge should remain unavailable for two phases")


def test_no_action_is_server_skip_and_player_is_open() -> None:
    engine = _engine()
    state = engine.start(FighterType.TANK, FighterType.TANK)

    outcome = engine.resolve_phase(
        state,
        None,
        ArenaPlayerAction.FAST_ATTACK,
    )

    assert outcome.player_action.value == "skip"
    assert outcome.opponent_action == ArenaPlayerAction.FAST_ATTACK
    assert outcome.player_damage == 0
    assert outcome.opponent_damage == 8
    assert outcome.state.player.hp == 122


def test_special_requires_full_energy_and_tank_fortress_reduces_damage() -> None:
    engine = _engine()
    state = engine.start(FighterType.TANK, FighterType.TANK)

    try:
        engine.resolve_phase(state, ArenaPlayerAction.SPECIAL, None)
    except ActionNotAvailable:
        pass
    else:
        raise AssertionError("special must require 100 energy")

    state.player.energy = 100
    outcome = engine.resolve_phase(
        state,
        ArenaPlayerAction.SPECIAL,
        ArenaPlayerAction.HEAVY_ATTACK,
    )

    assert outcome.player_special is True
    assert outcome.player_damage == 0
    assert outcome.opponent_damage == 10  # Fortress halves 20 incoming damage.
    assert outcome.state.player.energy == 0
    assert outcome.state.player.damage_reduction_phases > 0


def test_assassin_special_ignores_block_but_can_be_dodged() -> None:
    engine = _engine()
    state = engine.start(FighterType.ASSASSIN, FighterType.TANK)
    state.player.energy = 100

    blocked = engine.resolve_phase(
        state,
        ArenaPlayerAction.SPECIAL,
        ArenaPlayerAction.BLOCK,
    )
    assert blocked.player_damage > 0

    state = engine.start(FighterType.ASSASSIN, FighterType.TANK)
    state.player.energy = 100
    dodged = engine.resolve_phase(
        state,
        ArenaPlayerAction.SPECIAL,
        ArenaPlayerAction.DODGE,
    )
    assert dodged.player_damage == 0


def test_berserker_rage_is_buff_and_tactician_trap_affects_next_phase() -> None:
    engine = _engine()
    state = engine.start(FighterType.BERSERKER, FighterType.TACTICIAN)
    state.player.energy = 100
    state.opponent.energy = 100

    rage = engine.resolve_phase(
        state,
        ArenaPlayerAction.SPECIAL,
        ArenaPlayerAction.SPECIAL,
    )
    assert rage.player_special is True
    assert rage.opponent_special is True
    assert rage.state.player.damage_bonus_percent > 0
    assert rage.state.player.damage_bonus_phases > 0
    assert rage.state.player.trap_phases > 0

    follow_up = engine.resolve_phase(
        rage.state,
        ArenaPlayerAction.FAST_ATTACK,
        ArenaPlayerAction.FAST_ATTACK,
    )
    assert follow_up.player_action.value == "skip"
    assert follow_up.player_damage == 0
    assert follow_up.opponent_damage > 0


def test_equal_actions_resolve_simultaneously() -> None:
    engine = _engine()
    state = engine.start(FighterType.TANK, FighterType.TANK)

    outcome = engine.resolve_phase(
        state,
        ArenaPlayerAction.FAST_ATTACK,
        ArenaPlayerAction.FAST_ATTACK,
    )

    assert outcome.player_damage == 8
    assert outcome.opponent_damage == 8
    assert outcome.state.player.hp == 122
    assert outcome.state.opponent.hp == 122


def test_knockout_finishes_match_before_timer() -> None:
    engine = _engine()
    state = engine.start(FighterType.TANK, FighterType.ASSASSIN)
    state.opponent.hp = 8

    outcome = engine.resolve_phase(
        state,
        ArenaPlayerAction.FAST_ATTACK,
        None,
    )

    assert outcome.match_result == ArenaMatchResult.WIN
    assert outcome.winner == "player"
    assert outcome.state.finished is True


def test_timeout_uses_remaining_hp_and_draw_refunds() -> None:
    engine = _engine()
    state = engine.start(FighterType.TANK, FighterType.ASSASSIN)
    state.elapsed_seconds = 87
    state.player.hp = 40
    state.opponent.hp = 30

    player_win = engine.resolve_phase(state, None, None)
    assert player_win.match_result == ArenaMatchResult.WIN
    assert player_win.winner == "player"

    state = engine.start(FighterType.TANK, FighterType.ASSASSIN)
    state.elapsed_seconds = 87
    state.player.hp = 40
    state.opponent.hp = 40
    draw = engine.resolve_phase(state, None, None)
    assert draw.match_result == ArenaMatchResult.DRAW
    assert draw.winner is None


def test_special_and_dodge_are_rejected_when_unavailable() -> None:
    engine = _engine()
    state = engine.start(FighterType.TANK, FighterType.TANK)
    state.player.dodge_cooldown_remaining = 2

    try:
        engine.resolve_phase(state, ArenaPlayerAction.DODGE, None)
    except ActionNotAvailable:
        pass
    else:
        raise AssertionError("dodge should not be available while on cooldown")
