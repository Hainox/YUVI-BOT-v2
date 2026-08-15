from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from common.arena.config import ArenaConfig
from common.arena.schemas import ArenaMatchResult
from common.arena.schemas import ArenaMatchStatus
from common.arena.schemas import ArenaPlayerAction
from common.arena.schemas import ArenaResolvedAction
from common.arena.schemas import FighterType


def test_default_arena_config_matches_approved_rules() -> None:
    config = ArenaConfig()

    assert config.match_duration_seconds == 90
    assert config.phase_duration_seconds == 3
    assert config.phase_count == 30
    assert config.min_bet == 100
    assert config.fee_percent == Decimal("5")
    assert config.fee_split_percent == Decimal("2.5")
    assert config.open_match_ttl_seconds == 5 * 60
    assert config.accept_confirmation_seconds == 15
    assert config.disconnect_grace_seconds == 15
    assert config.dodge_cooldown_phases == 2
    assert config.max_energy == 100
    assert config.special_energy_cost == 100
    assert config.energy_regen_per_phase == 5
    assert config.energy_action_bonus == 5
    assert config.energy_damage_bonus == 5
    assert config.max_fighter_level == 50
    assert config.base_match_xp == 100
    assert config.win_bonus_xp == 50
    assert config.initial_rating == 1000


def test_arena_config_rejects_invalid_timing_energy_and_money_values() -> None:
    invalid_configs = (
        {"match_duration_seconds": 0},
        {"phase_duration_seconds": 0},
        {"match_duration_seconds": 90, "phase_duration_seconds": 7},
        {"match_duration_seconds": 90, "phase_duration_seconds": 91},
        {"min_bet": 99},
        {"fee_percent": 101},
        {"special_energy_cost": 101},
    )

    for values in invalid_configs:
        with pytest.raises(ValidationError):
            ArenaConfig(**values)


def test_arena_config_rejects_extra_fields() -> None:
    with pytest.raises(ValidationError):
        ArenaConfig(unknown_setting=123)


def test_client_cannot_submit_server_generated_skip() -> None:
    assert "skip" not in {action.value for action in ArenaPlayerAction}
    assert "skip" in {action.value for action in ArenaResolvedAction}


def test_approved_domain_contracts_are_explicit() -> None:
    assert {fighter.value for fighter in FighterType} == {
        "tank",
        "assassin",
        "berserker",
        "tactician",
    }
    assert {status.value for status in ArenaMatchStatus} == {
        "waiting",
        "accepting",
        "active",
        "finished",
        "cancelled",
        "refunded",
    }
    assert {result.value for result in ArenaMatchResult} == {
        "win",
        "draw",
        "technical_loss",
    }


def test_config_is_not_mutated_by_accidental_assignment() -> None:
    config = ArenaConfig()

    with pytest.raises(ValidationError):
        config.phase_duration_seconds = 0
