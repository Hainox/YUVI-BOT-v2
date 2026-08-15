"""Random opponent-action picker for Arena's training mode (Phase 6,
FIGHTING_SPEC.md §9 / FIGHTING_ROADMAP.md "Фаза 6. Тренировочный режим").

Deliberately the ONLY new file this feature needs on top of `ArenaEngine`
itself — training mode is a fully separate, ephemeral (Redis-only, no
Postgres row, no stakes/rating/XP) 1-vs-AI wrapper around the exact same
combat engine PvP uses (`common/arena/engine.py`), so this module's only
job is "what does the AI do this phase", nothing else.
"""

from __future__ import annotations

from common.arena.config import ArenaConfig
from common.arena.engine import CombatantState
from common.arena.schemas import ArenaPlayerAction


def pick_ai_action(combatant: CombatantState, config: ArenaConfig, rng) -> ArenaPlayerAction:
    """Uniformly random among the AI's currently AVAILABLE actions —
    FIGHTING_SPEC.md §9: "AI случайно выбирает доступное действие; если
    выбранное действие недоступно, AI выбирает другое доступное случайно"
    (equivalent to filtering first, then picking once — not "retry on
    rejection", since the constraint set is small/fixed and known upfront).

    Only two of the five actions can ever be unavailable: DODGE (cooldown)
    and SPECIAL (energy) — FAST_ATTACK/HEAVY_ATTACK/BLOCK are always legal,
    so `available` is never empty."""
    available = [
        action
        for action in ArenaPlayerAction
        if not (action is ArenaPlayerAction.DODGE and combatant.dodge_cooldown_remaining > 0)
        and not (action is ArenaPlayerAction.SPECIAL and combatant.energy < config.special_energy_cost)
    ]
    return rng.choice(available)
