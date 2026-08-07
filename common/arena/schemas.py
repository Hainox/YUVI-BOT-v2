from __future__ import annotations

from enum import StrEnum


class ArenaPlayerAction(StrEnum):
    """Actions that a client may submit during a decision phase."""

    FAST_ATTACK = "fast_attack"
    HEAVY_ATTACK = "heavy_attack"
    BLOCK = "block"
    DODGE = "dodge"
    SPECIAL = "special"


class ArenaResolvedAction(StrEnum):
    """Actions after server resolution; SKIP is never client-submitted."""

    FAST_ATTACK = "fast_attack"
    HEAVY_ATTACK = "heavy_attack"
    BLOCK = "block"
    DODGE = "dodge"
    SPECIAL = "special"
    SKIP = "skip"


class FighterType(StrEnum):
    TANK = "tank"
    ASSASSIN = "assassin"
    BERSERKER = "berserker"
    TACTICIAN = "tactician"


class ArenaMatchStatus(StrEnum):
    """Lifecycle state; it does not describe who won."""

    WAITING = "waiting"
    ACCEPTING = "accepting"
    ACTIVE = "active"
    FINISHED = "finished"
    CANCELLED = "cancelled"
    REFUNDED = "refunded"


class ArenaMatchResult(StrEnum):
    """Server-authoritative outcome, recorded when a match finishes."""

    WIN = "win"
    DRAW = "draw"
    TECHNICAL_LOSS = "technical_loss"
