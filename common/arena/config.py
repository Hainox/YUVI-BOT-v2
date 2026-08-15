from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field
from pydantic import model_validator


class ArenaConfig(BaseModel):
    """Rules shared by the arena engine, API and clients.

    The model is deliberately pure and immutable: application settings may
    construct it later, while domain tests stay independent of bot settings.
    Financial rates use Decimal so a configurable half-fee never relies on
    binary floating-point arithmetic.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    match_duration_seconds: int = Field(default=90, gt=0)
    phase_duration_seconds: int = Field(default=3, gt=0)
    min_bet: int = Field(default=100, ge=100)
    fee_percent: Decimal = Field(default=Decimal("5"), ge=0, le=100)
    open_match_ttl_seconds: int = Field(default=5 * 60, gt=0)
    accept_confirmation_seconds: int = Field(default=15, gt=0)
    disconnect_grace_seconds: int = Field(default=15, gt=0)
    dodge_cooldown_phases: int = Field(default=2, ge=0)
    counter_damage_bonus_percent: int = Field(default=25, ge=0)
    max_energy: int = Field(default=100, gt=0)
    special_energy_cost: int = Field(default=100, gt=0)
    energy_regen_per_phase: int = Field(default=5, ge=0)
    energy_action_bonus: int = Field(default=5, ge=0)
    energy_damage_bonus: int = Field(default=5, ge=0)
    max_fighter_level: int = Field(default=50, gt=0)
    base_match_xp: int = Field(default=100, ge=0)
    win_bonus_xp: int = Field(default=50, ge=0)
    initial_rating: int = Field(default=1000, ge=0)
    win_rating_delta: int = Field(default=25, ge=0)
    loss_rating_delta: int = Field(default=20, ge=0)
    technical_loss_rating_delta: int = Field(default=30, ge=0)

    @model_validator(mode="after")
    def validate_cross_field_rules(self) -> ArenaConfig:
        if self.match_duration_seconds % self.phase_duration_seconds:
            raise ValueError("match duration must divide evenly into phases")
        if self.phase_duration_seconds > self.match_duration_seconds:
            raise ValueError("phase duration cannot exceed match duration")
        if self.special_energy_cost > self.max_energy:
            raise ValueError("special energy cost cannot exceed max energy")
        return self

    @property
    def phase_count(self) -> int:
        """Number of complete decision phases in a match."""
        return self.match_duration_seconds // self.phase_duration_seconds

    @property
    def fee_split_percent(self) -> Decimal:
        """Each of the two fee destinations receives half of the fee."""
        return self.fee_percent / Decimal("2")
