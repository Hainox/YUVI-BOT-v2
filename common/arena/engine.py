from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from decimal import Decimal
from decimal import ROUND_HALF_UP

from common.arena.config import ArenaConfig
from common.arena.fighters import FIGHTERS
from common.arena.fighters import FighterDefinition
from common.arena.schemas import ArenaMatchResult
from common.arena.schemas import ArenaPlayerAction
from common.arena.schemas import ArenaResolvedAction
from common.arena.schemas import FighterType


class ArenaEngineError(Exception):
    """Base error for invalid domain operations."""


class ActionNotAvailable(ArenaEngineError):
    """The requested action is on cooldown or lacks required energy."""


class MatchFinished(ArenaEngineError):
    """An action was submitted after the match had already finished."""


@dataclass(slots=True)
class CombatantState:
    fighter: FighterDefinition
    hp: int
    energy: int = 0
    dodge_cooldown_remaining: int = 0
    damage_reduction_phases: int = 0
    damage_bonus_percent: int = 0
    damage_bonus_phases: int = 0
    trap_phases: int = 0


@dataclass(slots=True)
class MatchState:
    player: CombatantState
    opponent: CombatantState
    elapsed_seconds: int = 0
    phase_index: int = 0
    finished: bool = False
    match_result: ArenaMatchResult | None = None
    winner: str | None = None


@dataclass(slots=True)
class MatchOutcome:
    """Result of one phase plus the next server state.

    The state is copied before resolution, so callers may safely retain the
    input state for replay/debugging. The object is intentionally not marked
    frozen because its nested state is a mutable domain snapshot.
    """

    state: MatchState
    player_action: ArenaResolvedAction
    opponent_action: ArenaResolvedAction
    player_damage: int
    opponent_damage: int
    player_counter: bool = False
    opponent_counter: bool = False
    player_special: bool = False
    opponent_special: bool = False

    @property
    def match_result(self) -> ArenaMatchResult | None:
        return self.state.match_result

    @property
    def winner(self) -> str | None:
        return self.state.winner


class ArenaEngine:
    """Deterministic phase resolver; no DB, network or internal RNG."""

    _COUNTERS: dict[tuple[ArenaResolvedAction, ArenaResolvedAction], str] = {
        (ArenaResolvedAction.FAST_ATTACK, ArenaResolvedAction.HEAVY_ATTACK): "player",
        (ArenaResolvedAction.HEAVY_ATTACK, ArenaResolvedAction.BLOCK): "player",
        (ArenaResolvedAction.BLOCK, ArenaResolvedAction.FAST_ATTACK): "player",
    }

    def __init__(self, config: ArenaConfig | None = None) -> None:
        self.config = config or ArenaConfig()

    def start(self, player_fighter: FighterType, opponent_fighter: FighterType) -> MatchState:
        return MatchState(
            player=self._new_combatant(player_fighter),
            opponent=self._new_combatant(opponent_fighter),
        )

    def resolve_phase(
        self,
        state: MatchState,
        player_action: ArenaPlayerAction | str | None,
        opponent_action: ArenaPlayerAction | str | None,
    ) -> MatchOutcome:
        if state.finished:
            raise MatchFinished("match has already finished")

        next_state = deepcopy(state)
        previous_player_trap = next_state.player.trap_phases
        previous_opponent_trap = next_state.opponent.trap_phases
        previous_player_reduction = next_state.player.damage_reduction_phases
        previous_opponent_reduction = next_state.opponent.damage_reduction_phases
        previous_player_bonus = next_state.player.damage_bonus_phases
        previous_opponent_bonus = next_state.opponent.damage_bonus_phases

        player_resolved = self._resolve_phase_action(
            next_state.player,
            player_action,
            trapped=previous_player_trap > 0,
        )
        opponent_resolved = self._resolve_phase_action(
            next_state.opponent,
            opponent_action,
            trapped=previous_opponent_trap > 0,
        )

        player_special = player_resolved is ArenaResolvedAction.SPECIAL
        opponent_special = opponent_resolved is ArenaResolvedAction.SPECIAL
        if player_special:
            next_state.player.energy = 0
        if opponent_special:
            next_state.opponent.energy = 0

        self._apply_special_effect(next_state.player, next_state.opponent, player_special)
        self._apply_special_effect(next_state.opponent, next_state.player, opponent_special)

        player_damage, opponent_damage, player_counter, opponent_counter = self._resolve_damage(
            next_state,
            player_resolved,
            opponent_resolved,
            player_special,
            opponent_special,
        )

        next_state.opponent.hp = max(0, next_state.opponent.hp - player_damage)
        next_state.player.hp = max(0, next_state.player.hp - opponent_damage)
        self._advance_resources(
            next_state,
            player_resolved,
            opponent_resolved,
            player_special,
            opponent_special,
            player_damage,
            opponent_damage,
            previous_player_reduction,
            previous_opponent_reduction,
            previous_player_bonus,
            previous_opponent_bonus,
            previous_player_trap,
            previous_opponent_trap,
        )

        next_state.phase_index += 1
        next_state.elapsed_seconds += self.config.phase_duration_seconds
        self._finish_if_needed(next_state)

        return MatchOutcome(
            state=next_state,
            player_action=player_resolved,
            opponent_action=opponent_resolved,
            player_damage=player_damage,
            opponent_damage=opponent_damage,
            player_counter=player_counter,
            opponent_counter=opponent_counter,
            player_special=player_special,
            opponent_special=opponent_special,
        )

    def _new_combatant(self, fighter_type: FighterType) -> CombatantState:
        try:
            fighter = FIGHTERS[fighter_type]
        except KeyError as exc:
            raise ArenaEngineError(f"unknown fighter: {fighter_type}") from exc
        return CombatantState(fighter=fighter, hp=fighter.max_hp)

    def _resolve_phase_action(
        self,
        combatant: CombatantState,
        action: ArenaPlayerAction | str | None,
        *,
        trapped: bool,
    ) -> ArenaResolvedAction:
        # Trap and timeout are server-generated skips. A client cannot submit
        # SKIP because ArenaPlayerAction intentionally has no such member.
        if trapped or action is None:
            return ArenaResolvedAction.SKIP
        try:
            normalized = ArenaPlayerAction(action)
        except (TypeError, ValueError) as exc:
            raise ArenaEngineError(f"unknown action: {action!r}") from exc
        if normalized is ArenaPlayerAction.DODGE and combatant.dodge_cooldown_remaining > 0:
            raise ActionNotAvailable("dodge is on cooldown")
        if normalized is ArenaPlayerAction.SPECIAL and combatant.energy < self.config.special_energy_cost:
            raise ActionNotAvailable("special requires full energy")
        return ArenaResolvedAction(normalized.value)

    def _apply_special_effect(
        self,
        owner: CombatantState,
        target: CombatantState,
        used: bool,
    ) -> None:
        if not used:
            return
        kind = owner.fighter.special_kind
        if kind == "fortress":
            # Active in the current phase and one following phase.
            owner.damage_reduction_phases = 1
        elif kind == "rage":
            owner.damage_bonus_percent = 50 if owner.hp * 2 <= owner.fighter.max_hp else 25
            owner.damage_bonus_phases = 1
        elif kind == "trap":
            # The target is skipped on the next phase, not this one.
            target.trap_phases = 1

    def _resolve_damage(
        self,
        state: MatchState,
        player_action: ArenaResolvedAction,
        opponent_action: ArenaResolvedAction,
        player_special: bool,
        opponent_special: bool,
    ) -> tuple[int, int, bool, bool]:
        player_counter = False
        opponent_counter = False

        player_dodges_opponent = player_action is ArenaResolvedAction.DODGE and (
            opponent_action in {
                ArenaResolvedAction.FAST_ATTACK,
                ArenaResolvedAction.HEAVY_ATTACK,
            }
            or opponent_special and state.opponent.fighter.special_kind == "dash"
        )
        opponent_dodges_player = opponent_action is ArenaResolvedAction.DODGE and (
            player_action in {
                ArenaResolvedAction.FAST_ATTACK,
                ArenaResolvedAction.HEAVY_ATTACK,
            }
            or player_special and state.player.fighter.special_kind == "dash"
        )

        player_damage = 0 if opponent_dodges_player else self._base_damage(
            state.player, player_action, player_special
        )
        opponent_damage = 0 if player_dodges_opponent else self._base_damage(
            state.opponent, opponent_action, opponent_special
        )

        if not player_special and not opponent_special and not player_dodges_opponent and not opponent_dodges_player:
            counter = self._COUNTERS.get((player_action, opponent_action))
            if counter == "player":
                player_counter = True
                opponent_damage = 0
            elif counter is None:
                reverse = self._COUNTERS.get((opponent_action, player_action))
                if reverse == "player":
                    opponent_counter = True
                    player_damage = 0

        if player_counter:
            player_damage = self._with_counter_bonus(player_damage)
        if opponent_counter:
            opponent_damage = self._with_counter_bonus(opponent_damage)

        player_damage = self._with_damage_bonus(state.player, player_damage)
        opponent_damage = self._with_damage_bonus(state.opponent, opponent_damage)

        if state.player.damage_reduction_phases > 0:
            opponent_damage = self._half(opponent_damage)
        if state.opponent.damage_reduction_phases > 0:
            player_damage = self._half(player_damage)

        return player_damage, opponent_damage, player_counter, opponent_counter

    def _base_damage(
        self,
        combatant: CombatantState,
        action: ArenaResolvedAction,
        used_special: bool,
    ) -> int:
        if used_special:
            if combatant.fighter.special_kind == "dash":
                return combatant.fighter.special_damage
            return 0
        if action is ArenaResolvedAction.HEAVY_ATTACK:
            return combatant.fighter.heavy_damage
        if action is ArenaResolvedAction.FAST_ATTACK:
            return combatant.fighter.fast_damage
        return 0

    def _advance_resources(
        self,
        state: MatchState,
        player_action: ArenaResolvedAction,
        opponent_action: ArenaResolvedAction,
        player_special: bool,
        opponent_special: bool,
        player_damage: int,
        opponent_damage: int,
        previous_player_reduction: int,
        previous_opponent_reduction: int,
        previous_player_bonus: int,
        previous_opponent_bonus: int,
        previous_player_trap: int,
        previous_opponent_trap: int,
    ) -> None:
        self._advance_combatant(
            state.player,
            player_action,
            player_special,
            opponent_damage,
            previous_player_reduction,
            previous_player_bonus,
            previous_player_trap,
        )
        self._advance_combatant(
            state.opponent,
            opponent_action,
            opponent_special,
            player_damage,
            previous_opponent_reduction,
            previous_opponent_bonus,
            previous_opponent_trap,
        )

    def _advance_combatant(
        self,
        combatant: CombatantState,
        action: ArenaResolvedAction,
        used_special: bool,
        damage_taken: int,
        previous_damage_reduction: int,
        previous_damage_bonus: int,
        previous_trap: int,
    ) -> None:
        if action is ArenaResolvedAction.DODGE:
            combatant.dodge_cooldown_remaining = self.config.dodge_cooldown_phases
        elif combatant.dodge_cooldown_remaining > 0:
            combatant.dodge_cooldown_remaining -= 1

        if previous_damage_reduction > 0:
            combatant.damage_reduction_phases -= 1
        if previous_damage_bonus > 0:
            combatant.damage_bonus_phases -= 1
            if combatant.damage_bonus_phases == 0:
                combatant.damage_bonus_percent = 0
        if previous_trap > 0:
            combatant.trap_phases -= 1

        if used_special:
            return

        energy_gain = self.config.energy_regen_per_phase
        if action in {
            ArenaResolvedAction.FAST_ATTACK,
            ArenaResolvedAction.HEAVY_ATTACK,
            ArenaResolvedAction.BLOCK,
            ArenaResolvedAction.DODGE,
        }:
            energy_gain += self.config.energy_action_bonus
        if damage_taken > 0:
            energy_gain += self.config.energy_damage_bonus
        combatant.energy = min(self.config.max_energy, combatant.energy + energy_gain)

    def _finish_if_needed(self, state: MatchState) -> None:
        if state.player.hp <= 0 and state.opponent.hp <= 0:
            state.finished = True
            state.match_result = ArenaMatchResult.DRAW
            return
        if state.opponent.hp <= 0:
            state.finished = True
            state.match_result = ArenaMatchResult.WIN
            state.winner = "player"
            return
        if state.player.hp <= 0:
            state.finished = True
            state.match_result = ArenaMatchResult.WIN
            state.winner = "opponent"
            return
        if state.elapsed_seconds < self.config.match_duration_seconds:
            return
        state.finished = True
        state.match_result = ArenaMatchResult.DRAW if state.player.hp == state.opponent.hp else ArenaMatchResult.WIN
        if state.player.hp > state.opponent.hp:
            state.winner = "player"
        elif state.opponent.hp > state.player.hp:
            state.winner = "opponent"

    def _with_counter_bonus(self, damage: int) -> int:
        factor = Decimal(100 + self.config.counter_damage_bonus_percent) / Decimal(100)
        return int((Decimal(damage) * factor).quantize(Decimal("1"), rounding=ROUND_HALF_UP))

    @staticmethod
    def _with_damage_bonus(combatant: CombatantState, damage: int) -> int:
        if damage == 0 or combatant.damage_bonus_percent == 0:
            return damage
        factor = Decimal(100 + combatant.damage_bonus_percent) / Decimal(100)
        return int((Decimal(damage) * factor).quantize(Decimal("1"), rounding=ROUND_HALF_UP))

    @staticmethod
    def _half(damage: int) -> int:
        return int((Decimal(damage) / Decimal(2)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
