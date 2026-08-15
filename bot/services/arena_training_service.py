"""Arena training mode (Phase 6, FIGHTING_SPEC.md §9) — practice against a
random AI opponent, no stakes, no rating, no XP.

Deliberately fully separate from the PvP lobby/runtime (`arena_service.py`/
`arena_session_service.py`): no `ArenaMatch` row, no Postgres at all, no
`economy_service` call anywhere in this module — Redis-only, ephemeral
(TTL-bounded) state keyed by `(chat_id, user_id)`, one session per player.
This is a deliberate design choice, not an oversight: touching the PvP
lobby/settlement code (`ArenaMatch`, `create_match`/`settle_match`) to bolt
on a `match_type="training"` bypass would mean re-touching code that was
just adversarially reviewed and shipped as a money-safety fix, for a mode
that FIGHTING_SPEC.md §9 explicitly defines as having NO money/rating/XP
consequence at all — a parallel, engine-only implementation has zero
surface area to get that bypass wrong, because there's no economy code
present to bypass in the first place.

Reuses `ArenaEngine` (the actual "unified combat engine" FIGHTING_ROADMAP.md
Phase 6 requires) and `arena_session_service`'s private engine (de)serializer
helpers (`_engine_to_dict`/`_engine_from_dict` — read-only reuse, this module
never imports or calls anything else from that file, so it cannot destabilize
the PvP runtime it wasn't reviewed alongside).

Unlike PvP, there is no phase-deadline/tick loop: the AI "submits" its
action synchronously the instant the human submits theirs (no second real
player to wait for), so one `submit_action` call fully resolves one phase
and returns the outcome — no scheduled worker needed for this feature.
"""

from __future__ import annotations

import json
import logging
import secrets
from datetime import datetime
from datetime import timezone
from typing import Any

from bot.services.arena_session_service import _engine_from_dict
from bot.services.arena_session_service import _engine_to_dict
from common.arena.config import ArenaConfig
from common.arena.engine import ActionNotAvailable
from common.arena.engine import ArenaEngine
from common.arena.engine import MatchFinished
from common.arena.schemas import ArenaPlayerAction
from common.arena.schemas import FighterType
from common.arena.training_ai import pick_ai_action

logger = logging.getLogger(__name__)

# Short-lived by design (Claude's Discretion — no spec-mandated value): a
# forgotten training session carries no money/rating risk, so this only
# bounds how long stale Redis keys linger, not any gameplay-critical window.
_SESSION_TTL_SECONDS = 60 * 60


class ArenaTrainingError(Exception):
    """Base error for training-session operations."""


class TrainingSessionNotFound(ArenaTrainingError):
    """No active training session exists for this (chat_id, user_id)."""


class TrainingSessionPaused(ArenaTrainingError):
    """An action was submitted while the session is paused."""


class TrainingActionNotAvailable(ArenaTrainingError):
    """The human's chosen action is on cooldown or lacks required energy."""


class ArenaTrainingService:
    def __init__(self, redis_client, *, engine: ArenaEngine | None = None, config: ArenaConfig | None = None) -> None:
        self.redis = redis_client
        self.config = config or ArenaConfig()
        self.engine = engine or ArenaEngine(self.config)
        self._rng = secrets.SystemRandom()

    @staticmethod
    def session_key(chat_id: int, user_id: int) -> str:
        return f"arena:training:{chat_id}:{user_id}"

    @staticmethod
    def lock_key(chat_id: int, user_id: int) -> str:
        return f"arena:training:lock:{chat_id}:{user_id}"

    def _session_lock(self, chat_id: int, user_id: int):
        # Same read-mutate-write-under-lock discipline as
        # arena_session_service.ArenaSessionService._match_lock — without
        # it, two concurrent requests for the same (chat_id, user_id) (e.g.
        # a client retry racing the original, or two open tabs) could both
        # read the same phase_index, both resolve independently, and the
        # second Redis SET would silently clobber the first's outcome
        # (last-writer-wins on a full-value overwrite, no corruption, but a
        # dropped action/phase — worth closing even though this is a
        # single-player, no-money feature).
        return self.redis.lock(self.lock_key(chat_id, user_id), timeout=10, blocking_timeout=10)

    async def start_or_restart(
        self,
        chat_id: int,
        user_id: int,
        fighter: FighterType | str,
        opponent_fighter: FighterType | str | None = None,
        *,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Always starts fresh — FIGHTING_ROADMAP.md Phase 6 "Проверка":
        "restart очищает старое состояние", so there is no "resume the old
        session" branch to get wrong (unlike PvP's `start_or_get`, which
        must resume — training has no stake to protect by resuming)."""
        fighter_value = _validate_fighter(fighter)
        opponent_value = (
            _validate_fighter(opponent_fighter)
            if opponent_fighter is not None
            else self._rng.choice(list(FighterType)).value
        )
        engine_state = self.engine.start(FighterType(fighter_value), FighterType(opponent_value))
        current = _iso(now)
        state = {
            "chat_id": chat_id,
            "user_id": user_id,
            "fighters": {"player": fighter_value, "opponent": opponent_value},
            "engine": _engine_to_dict(engine_state, max_energy=self.config.max_energy),
            "phase_index": 0,
            "paused": False,
            "terminal": False,
            "result": None,
            "winner": None,
            "last_outcome": None,
            "created_at": current,
            "updated_at": current,
        }
        async with self._session_lock(chat_id, user_id):
            await self._write(chat_id, user_id, state)
        return state

    async def get_state(self, chat_id: int, user_id: int) -> dict[str, Any]:
        return await self._read_required(chat_id, user_id)

    async def set_paused(self, chat_id: int, user_id: int, paused: bool) -> dict[str, Any]:
        async with self._session_lock(chat_id, user_id):
            state = await self._read_required(chat_id, user_id)
            if state["terminal"]:
                return state
            state["paused"] = paused
            await self._write(chat_id, user_id, state)
            return state

    async def submit_action(
        self,
        chat_id: int,
        user_id: int,
        action: ArenaPlayerAction | str,
        *,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        async with self._session_lock(chat_id, user_id):
            state = await self._read_required(chat_id, user_id)
            if state["terminal"]:
                return state
            if state["paused"]:
                raise TrainingSessionPaused(
                    "Тренировка на паузе — снимите с паузы, чтобы продолжить"
                )

            # Validate the action string BEFORE calling the engine (same
            # two-stage order as arena_session_service.submit_action) — this
            # way the only exception the engine call below can raise is a
            # genuine ActionNotAvailable (cooldown/energy), never "malformed
            # input", so the except clause doesn't need to guess which one
            # it caught.
            try:
                normalized_action = ArenaPlayerAction(action).value
            except (TypeError, ValueError) as exc:
                raise TrainingActionNotAvailable(f"Недопустимое действие: {action!r}") from exc

            engine_state = _engine_from_dict(state["engine"])
            ai_action = pick_ai_action(engine_state.opponent, self.config, self._rng)
            try:
                outcome = self.engine.resolve_phase(engine_state, normalized_action, ai_action.value)
            except MatchFinished:
                # Defensive — `state["terminal"]` above should already have
                # caught this; the engine's own guard is the authoritative
                # one.
                state["terminal"] = True
                return state
            except ActionNotAvailable as exc:
                # Dodge on cooldown / special without full energy — the
                # AI's own action is always pre-filtered valid by
                # `pick_ai_action`, so this can only be about the player's
                # submitted action.
                raise TrainingActionNotAvailable(str(exc)) from exc

            current = _iso(now)
            state["engine"] = _engine_to_dict(outcome.state, max_energy=self.config.max_energy)
            state["phase_index"] = outcome.state.phase_index
            state["updated_at"] = current
            state["last_outcome"] = {
                "phase_index": state["phase_index"] - 1,
                "player_action": outcome.player_action.value,
                "opponent_action": outcome.opponent_action.value,
                "player_damage": outcome.player_damage,
                "opponent_damage": outcome.opponent_damage,
                "player_counter": outcome.player_counter,
                "opponent_counter": outcome.opponent_counter,
            }
            if outcome.state.finished:
                state["terminal"] = True
                state["result"] = outcome.match_result.value if outcome.match_result else None
                state["winner"] = outcome.winner
            await self._write(chat_id, user_id, state)
            return state

    async def _read_required(self, chat_id: int, user_id: int) -> dict[str, Any]:
        raw = await self.redis.get(self.session_key(chat_id, user_id))
        if raw is None:
            raise TrainingSessionNotFound("Тренировочная сессия не найдена — начните новую")
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        return json.loads(raw)

    async def _write(self, chat_id: int, user_id: int, state: dict[str, Any]) -> None:
        await self.redis.set(
            self.session_key(chat_id, user_id),
            json.dumps(state, separators=(",", ":"), ensure_ascii=False),
            ex=_SESSION_TTL_SECONDS,
        )


def _validate_fighter(fighter: FighterType | str) -> str:
    try:
        return FighterType(fighter).value
    except (TypeError, ValueError) as exc:
        raise ArenaTrainingError("Неизвестный боец") from exc


def _iso(value: datetime | None) -> str:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc).isoformat()
