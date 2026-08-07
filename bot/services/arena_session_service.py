"""Server-authoritative runtime for an active Yuvi Arena match.

The service deliberately owns no money and no database transaction. Phase 5
settlement consumes the terminal snapshot produced here. Redis is the durable
runtime store; a short per-match lock serializes competing action/tick/
connectivity requests so a phase can resolve exactly once.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from datetime import timedelta
from datetime import timezone
import json
import logging
from typing import Any

from common.arena.config import ArenaConfig
from common.arena.engine import ArenaEngine
from common.arena.engine import CombatantState
from common.arena.engine import MatchState
from common.arena.schemas import ArenaMatchResult
from common.arena.schemas import ArenaPlayerAction
from common.arena.schemas import FighterType

logger = logging.getLogger(__name__)


class ArenaSessionError(Exception):
    """Base error for active-match runtime operations."""


class SessionNotFound(ArenaSessionError):
    """No active Redis session exists for the requested match."""


class SessionConflict(ArenaSessionError):
    """The requested operation conflicts with the authoritative state."""


class DuplicateAction(SessionConflict):
    """The client action id was already accepted or resolved."""


class UnauthorizedSession(SessionConflict):
    """The actor is not one of the two match participants."""


class InvalidAction(SessionConflict):
    """The client submitted an action outside the public action contract."""


class ArenaSessionService:
    """Redis runtime for one or more active Arena matches.

    ``redis_client`` only needs the small redis-py async surface used below:
    ``get``, ``set``, ``publish`` and ``lock``. This keeps unit tests pure and
    allows Redis failure to be surfaced as a session error rather than silently
    inventing a combat result.
    """

    _SESSION_TTL_SECONDS = 60 * 60 * 2

    def __init__(
        self,
        redis_client,
        *,
        engine: ArenaEngine | None = None,
        config: ArenaConfig | None = None,
        persist_state=None,
    ) -> None:
        self.redis = redis_client
        self.config = config or ArenaConfig()
        self.engine = engine or ArenaEngine(self.config)
        self.persist_state = persist_state

    @staticmethod
    def session_key(match_id: int, chat_id: int) -> str:
        return f"arena:session:{chat_id}:{match_id}"

    @staticmethod
    def event_channel(match_id: int, chat_id: int) -> str:
        return f"arena:match:{chat_id}:{match_id}"

    @staticmethod
    def lock_key(match_id: int, chat_id: int) -> str:
        return f"arena:session:lock:{chat_id}:{match_id}"

    async def start_or_get(self, match, *, now: datetime | None = None) -> dict[str, Any]:
        """Create the runtime snapshot for an active DB match, or return it."""
        self._require_active_match(match)
        current = _utc(now)
        key = self.session_key(match.id, match.chat_id)
        async with self._match_lock(match):
            existing = await self._read(key)
            if existing is not None:
                return existing

            restored = self._restore_from_match(match)
            if restored is not None:
                await self._write(key, restored)
                return restored

            state = self.engine.start(
                FighterType(match.creator_fighter), FighterType(match.opponent_fighter)
            )
            snapshot = self._new_snapshot(match, state, current)
            await self._write(key, snapshot)
            await self._persist(match, snapshot)
            await self._publish(match, "match_started", snapshot)
            return snapshot

    async def get_state(self, match, *, viewer_id: int) -> dict[str, Any]:
        state = await self._read(self.session_key(match.id, match.chat_id))
        if state is None:
            state = self._restore_from_match(match)
        if state is None:
            raise SessionNotFound(f"Активная сессия матча #{match.id} не найдена")
        self._require_participant(state, viewer_id)
        return self.public_state(state, viewer_id)

    @staticmethod
    def public_state(state: dict[str, Any], viewer_id: int) -> dict[str, Any]:
        """Return a viewer-safe copy without the opponent's pending choice."""
        import copy

        ArenaSessionService._require_participant(state, viewer_id)
        public = copy.deepcopy(state)
        public["viewer_id"] = viewer_id
        public["viewer_side"] = ArenaSessionService._side(state, viewer_id)
        public["actions"] = {
            user_id: entry
            for user_id, entry in state["actions"].items()
            if int(user_id) == viewer_id
        }
        public["processed_actions"] = {
            action_id: entry
            for action_id, entry in state["processed_actions"].items()
            if entry.get("user_id") == viewer_id
        }
        return public

    async def submit_action(
        self,
        match,
        user_id: int,
        action: ArenaPlayerAction | str,
        client_action_id: str,
        *,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        if not client_action_id or len(client_action_id) > 120:
            raise InvalidAction("Некорректный client_action_id")
        try:
            normalized_action = ArenaPlayerAction(action).value
        except (TypeError, ValueError) as exc:
            raise InvalidAction("Недопустимое действие") from exc

        current = _utc(now)
        async with self._match_lock(match):
            state = await self._read_required(match)
            self._require_participant(state, user_id)
            processed = state["processed_actions"].get(client_action_id)
            if processed is not None:
                if processed.get("user_id") == user_id:
                    # Safe retry after a lost response: return the current
                    # authoritative snapshot instead of making the client
                    # invent a second action for the same phase.
                    return self.public_state(state, user_id)
                raise DuplicateAction("Идентификатор действия уже занят")
            if state["terminal"]:
                raise SessionConflict("Матч уже завершён")

            if current >= _parse_time(state["phase_deadline"]):
                outcomes = self._resolve_expired_phases(state, current)
                await self._write(self.session_key(match.id, match.chat_id), state)
                await self._persist(match, state)
                for outcome in outcomes:
                    await self._publish(match, "phase_resolved", outcome)
                if state["terminal"]:
                    raise SessionConflict("Матч завершён до получения действия")
                raise SessionConflict("Фаза уже закрыта")

            if str(user_id) in state["actions"]:
                raise DuplicateAction("У игрока уже есть действие в этой фазе")

            state["processed_actions"][client_action_id] = {
                "user_id": user_id,
                "phase_index": state["phase_index"],
                "action": normalized_action,
            }
            action_entry = {
                "action": normalized_action,
                "client_action_id": client_action_id,
            }

            state["actions"][str(user_id)] = action_entry
            outcomes: list[dict[str, Any]] = []
            if len(state["actions"]) == 2:
                outcomes.append(self._resolve_current_phase(state, current))
            else:
                state["updated_at"] = _iso(current)

            await self._write(self.session_key(match.id, match.chat_id), state)
            await self._persist(match, state)
            for outcome in outcomes:
                await self._publish(match, "phase_resolved", outcome)
            if state["terminal"]:
                await self._publish(match, "match_terminal", state)
            return self.public_state(state, user_id)

    async def tick(self, match, *, now: datetime | None = None) -> dict[str, Any]:
        """Close an expired phase or disconnected player deadline."""
        current = _utc(now)
        async with self._match_lock(match):
            state = await self._read_required(match)
            if state["terminal"]:
                return state
            self._expire_disconnects(state, current)
            outcomes: list[dict[str, Any]] = []
            if not state["terminal"] and current >= _parse_time(state["phase_deadline"]):
                outcomes = self._resolve_expired_phases(state, current)
            state["updated_at"] = _iso(current)
            await self._write(self.session_key(match.id, match.chat_id), state)
            await self._persist(match, state)
            for outcome in outcomes:
                await self._publish(match, "phase_resolved", outcome)
            if state["terminal"]:
                await self._publish(match, "match_terminal", state)
            return state

    async def disconnect(self, match, user_id: int, *, now: datetime | None = None) -> dict[str, Any]:
        current = _utc(now)
        async with self._match_lock(match):
            state = await self._read_required(match)
            self._require_participant(state, user_id)
            if state["terminal"]:
                return state
            connection = state["connections"][str(user_id)]
            connection["connected"] = False
            connection["deadline"] = _iso(current + timedelta(seconds=self.config.disconnect_grace_seconds))
            state["updated_at"] = _iso(current)
            await self._write(self.session_key(match.id, match.chat_id), state)
            await self._persist(match, state)
            await self._publish(match, "player_disconnected", state)
            return self.public_state(state, user_id)

    async def reconnect(self, match, user_id: int, *, now: datetime | None = None) -> dict[str, Any]:
        current = _utc(now)
        async with self._match_lock(match):
            state = await self._read_required(match)
            self._require_participant(state, user_id)
            if state["terminal"]:
                return state
            self._expire_disconnects(state, current)
            if state["terminal"]:
                await self._write(self.session_key(match.id, match.chat_id), state)
                await self._persist(match, state)
                return self.public_state(state, user_id)
            connection = state["connections"][str(user_id)]
            if not connection["connected"] and connection["deadline"] is not None and current >= _parse_time(connection["deadline"]):
                raise SessionConflict("Срок переподключения истёк")
            state["connections"][str(user_id)] = {"connected": True, "deadline": None}
            state["updated_at"] = _iso(current)
            await self._write(self.session_key(match.id, match.chat_id), state)
            await self._persist(match, state)
            await self._publish(match, "player_reconnected", state)
            return self.public_state(state, user_id)

    async def forfeit(self, match, user_id: int, *, now: datetime | None = None) -> dict[str, Any]:
        current = _utc(now)
        async with self._match_lock(match):
            state = await self._read_required(match)
            self._require_participant(state, user_id)
            if state["terminal"]:
                return self.public_state(state, user_id)
            winner_id = self._other_user(state, user_id)
            state.update(
                terminal=True,
                result=ArenaMatchResult.TECHNICAL_LOSS.value,
                winner_id=winner_id,
                loser_id=user_id,
                reason="forfeit",
                refund_required=False,
                finished_at=_iso(current),
                updated_at=_iso(current),
            )
            await self._write(self.session_key(match.id, match.chat_id), state)
            await self._persist(match, state)
            await self._publish(match, "match_terminal", state)
            return self.public_state(state, user_id)

    async def _read_required(self, match) -> dict[str, Any]:
        state = await self._read(self.session_key(match.id, match.chat_id))
        if state is None:
            state = self._restore_from_match(match)
        if state is None:
            raise SessionNotFound(f"Активная сессия матча #{match.id} не найдена")
        return state

    def _restore_from_match(self, match) -> dict[str, Any] | None:
        replay_data = getattr(match, "replay_data", None)
        if not isinstance(replay_data, dict):
            return None
        runtime_state = replay_data.get("runtime_state")
        if not isinstance(runtime_state, dict):
            return None

        # Snapshots may outlive a deploy. Fill fields introduced by later
        # runtime versions and derive fighter-specific HUD limits from the
        # authoritative fighter definition instead of using a client fallback.
        import copy
        from common.arena.fighters import FIGHTERS

        state = copy.deepcopy(runtime_state)
        state.setdefault("phase_duration_seconds", self.config.phase_duration_seconds)
        state.setdefault("actions", {})
        state.setdefault("processed_actions", {})
        state.setdefault("outcome_history", [])
        state.setdefault("last_outcome", None)
        state.setdefault("terminal", False)
        state.setdefault("refund_required", False)
        state.setdefault("connections", {})
        for side in ("player", "opponent"):
            combatant = state.get("engine", {}).get(side, {})
            fighter_value = combatant.get("fighter") or state.get("fighters", {}).get(side)
            if fighter_value is not None:
                fighter = FIGHTERS[FighterType(fighter_value)]
                combatant["fighter"] = fighter.fighter_type.value
                combatant["max_hp"] = fighter.max_hp
            combatant.setdefault("max_energy", self.config.max_energy)
            combatant.setdefault("dodge_cooldown_remaining", 0)
            combatant.setdefault("damage_reduction_phases", 0)
            combatant.setdefault("damage_bonus_percent", 0)
            combatant.setdefault("damage_bonus_phases", 0)
            combatant.setdefault("trap_phases", 0)
            state.setdefault("engine", {})[side] = combatant
        return state

    async def _read(self, key: str) -> dict[str, Any] | None:
        raw = await self.redis.get(key)
        if raw is None:
            return None
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        if isinstance(raw, dict):
            return raw
        return json.loads(raw)

    async def _write(self, key: str, state: dict[str, Any]) -> None:
        await self.redis.set(key, json.dumps(state, separators=(",", ":"), ensure_ascii=False), ex=self._SESSION_TTL_SECONDS)

    async def _publish(self, match, event_type: str, payload: dict[str, Any]) -> None:
        try:
            event_payload = payload
            if event_type != "phase_resolved":
                import copy

                event_payload = copy.deepcopy(payload)
                event_payload.pop("actions", None)
                event_payload.pop("processed_actions", None)
            envelope = {"event_type": event_type, "match_id": match.id, "chat_id": match.chat_id, "payload": event_payload}
            await self.redis.publish(self.event_channel(match.id, match.chat_id), json.dumps(envelope, ensure_ascii=False))
        except Exception:  # Redis live events must not corrupt authoritative state
            logger.debug("Arena event publish failed", exc_info=True)

    async def _persist(self, match, state: dict[str, Any]) -> None:
        if self.persist_state is None:
            return
        try:
            await asyncio.wait_for(self.persist_state(match, state), timeout=2.0)
        except Exception:  # Redis runtime remains authoritative during DB outages
            logger.exception("Arena runtime snapshot persistence failed match_id=%s", match.id)

    def _new_snapshot(self, match, engine_state: MatchState, now: datetime) -> dict[str, Any]:
        return {
            "match_id": match.id,
            "chat_id": match.chat_id,
            "players": {"player": match.creator_id, "opponent": match.opponent_id},
            "fighters": {"player": match.creator_fighter, "opponent": match.opponent_fighter},
            "phase_index": 0,
            "phase_started_at": _iso(now),
            "phase_deadline": _iso(now + timedelta(seconds=self.config.phase_duration_seconds)),
            "phase_duration_seconds": self.config.phase_duration_seconds,
            "actions": {},
            "processed_actions": {},
            "connections": {
                str(match.creator_id): {"connected": True, "deadline": None},
                str(match.opponent_id): {"connected": True, "deadline": None},
            },
            "engine": _engine_to_dict(engine_state, max_energy=self.config.max_energy),
            "allowed_actions": [action.value for action in ArenaPlayerAction],
            "last_outcome": None,
            "outcome_history": [],
            "terminal": False,
            "result": None,
            "winner_id": None,
            "loser_id": None,
            "reason": None,
            "refund_required": False,
            "started_at": _iso(now),
            "finished_at": None,
            "updated_at": _iso(now),
        }

    def _resolve_expired_phases(self, state: dict[str, Any], now: datetime) -> list[dict[str, Any]]:
        """Catch up every elapsed 3-second phase, generating server skips."""
        outcomes: list[dict[str, Any]] = []
        while not state["terminal"] and now >= _parse_time(state["phase_deadline"]):
            outcomes.append(self._resolve_current_phase(state, _parse_time(state["phase_deadline"])))
        return outcomes

    def _resolve_current_phase(self, state: dict[str, Any], now: datetime) -> dict[str, Any]:
        player_id = str(state["players"]["player"])
        opponent_id = str(state["players"]["opponent"])
        player_entry = state["actions"].get(player_id)
        opponent_entry = state["actions"].get(opponent_id)
        outcome = self.engine.resolve_phase(
            _engine_from_dict(state["engine"]),
            player_entry["action"] if player_entry else None,
            opponent_entry["action"] if opponent_entry else None,
        )
        old_phase = state["phase_index"]
        state["engine"] = _engine_to_dict(outcome.state, max_energy=self.config.max_energy)
        state["phase_index"] = outcome.state.phase_index
        state["phase_started_at"] = _iso(now)
        state["phase_deadline"] = _iso(now + timedelta(seconds=self.config.phase_duration_seconds))
        state["actions"] = {}
        state["updated_at"] = _iso(now)
        state["last_outcome"] = {
            "event_type": "phase_resolved",
            "phase_index": old_phase,
            "player_action": outcome.player_action.value,
            "opponent_action": outcome.opponent_action.value,
            "player_damage": outcome.player_damage,
            "opponent_damage": outcome.opponent_damage,
            "player_counter": outcome.player_counter,
            "opponent_counter": outcome.opponent_counter,
        }
        state.setdefault("outcome_history", []).append(state["last_outcome"])
        state["outcome_history"] = state["outcome_history"][-120:]
        if outcome.state.finished:
            winner_id = None
            loser_id = None
            if outcome.winner == "player":
                winner_id, loser_id = state["players"]["player"], state["players"]["opponent"]
            elif outcome.winner == "opponent":
                winner_id, loser_id = state["players"]["opponent"], state["players"]["player"]
            state.update(
                terminal=True,
                result=outcome.match_result.value if outcome.match_result else None,
                winner_id=winner_id,
                loser_id=loser_id,
                reason="knockout" if outcome.state.elapsed_seconds < self.config.match_duration_seconds else "time_limit",
                finished_at=_iso(now),
            )
        return state["last_outcome"]

    def _expire_disconnects(self, state: dict[str, Any], now: datetime) -> None:
        if state["terminal"]:
            return
        disconnected = [
            int(user_id)
            for user_id, connection in state["connections"].items()
            if not connection["connected"]
            and connection["deadline"] is not None
            and now >= _parse_time(connection["deadline"])
        ]
        if not disconnected:
            return
        connections = state["connections"]
        connected = [user_id for user_id, connection in connections.items() if connection["connected"]]
        if connected:
            loser_id = disconnected[0]
            state.update(
                terminal=True,
                result=ArenaMatchResult.TECHNICAL_LOSS.value,
                winner_id=self._other_user(state, loser_id),
                loser_id=loser_id,
                reason="disconnect_timeout",
                refund_required=False,
                finished_at=_iso(now),
                updated_at=_iso(now),
            )
            return
        if not any(connection["connected"] for connection in connections.values()):
            deadlines = [
                _parse_time(connection["deadline"])
                for connection in connections.values()
                if connection["deadline"] is not None
            ]
            if deadlines and all(now >= deadline for deadline in deadlines):
                self._mark_both_disconnected(state, now)
            return
    @staticmethod
    def _mark_both_disconnected(state: dict[str, Any], now: datetime) -> None:
        state.update(
            terminal=True,
            result=None,
            winner_id=None,
            loser_id=None,
            reason="both_disconnected",
            refund_required=True,
            finished_at=_iso(now),
            updated_at=_iso(now),
        )

    @staticmethod
    def _require_active_match(match) -> None:
        if match.status != "active" or match.opponent_id is None or match.opponent_fighter is None:
            raise SessionConflict("Матч не активен")

    @staticmethod
    def _require_participant(state: dict[str, Any], user_id: int) -> None:
        if user_id not in {state["players"]["player"], state["players"]["opponent"]}:
            raise UnauthorizedSession("Вы не участник этого матча")

    @staticmethod
    def _side(state: dict[str, Any], user_id: int) -> str:
        return "player" if user_id == state["players"]["player"] else "opponent"

    @staticmethod
    def _other_user(state: dict[str, Any], user_id: int) -> int:
        return state["players"]["opponent"] if user_id == state["players"]["player"] else state["players"]["player"]

    def _match_lock(self, match):
        return self.redis.lock(self.lock_key(match.id, match.chat_id), timeout=10, blocking_timeout=10)


def _utc(value: datetime | None) -> datetime:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        return current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return _utc(value).isoformat()


def _parse_time(value: str) -> datetime:
    return _utc(datetime.fromisoformat(value))


def _engine_to_dict(state: MatchState, *, max_energy: int = 100) -> dict[str, Any]:
    def combatant_to_dict(combatant: CombatantState) -> dict[str, Any]:
        return {
            "fighter": combatant.fighter.fighter_type.value,
            "hp": combatant.hp,
            "max_hp": combatant.fighter.max_hp,
            "max_energy": max_energy,
            "energy": combatant.energy,
            "dodge_cooldown_remaining": combatant.dodge_cooldown_remaining,
            "damage_reduction_phases": combatant.damage_reduction_phases,
            "damage_bonus_percent": combatant.damage_bonus_percent,
            "damage_bonus_phases": combatant.damage_bonus_phases,
            "trap_phases": combatant.trap_phases,
        }

    return {
        "player": combatant_to_dict(state.player),
        "opponent": combatant_to_dict(state.opponent),
        "elapsed_seconds": state.elapsed_seconds,
        "phase_index": state.phase_index,
        "finished": state.finished,
        "match_result": state.match_result.value if state.match_result else None,
        "winner": state.winner,
    }


async def persist_runtime_state(match, state: dict[str, Any]) -> None:
    """Persist the latest runtime snapshot for Redis-loss recovery.

    The callback opens a short independent DB transaction, so the combat lock
    never spans PostgreSQL I/O and API request sessions can safely be closed.
    """
    from sqlalchemy import select

    from common.db.session import SessionLocal
    from common.models.arena import ArenaMatch

    async with SessionLocal() as session:
        stored = (
            await session.execute(
                select(ArenaMatch)
                .where(ArenaMatch.id == match.id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if stored is None:
            return
        replay_data = dict(stored.replay_data or {})
        replay_data["runtime_state"] = state
        stored.replay_data = replay_data
        stored.phase_count = state.get("phase_index", stored.phase_count)
        await session.commit()


def _engine_from_dict(payload: dict[str, Any]) -> MatchState:
    from common.arena.fighters import FIGHTERS

    def combatant_from_dict(value: dict[str, Any]) -> CombatantState:
        return CombatantState(
            fighter=FIGHTERS[FighterType(value["fighter"])],
            hp=value["hp"],
            energy=value["energy"],
            dodge_cooldown_remaining=value["dodge_cooldown_remaining"],
            damage_reduction_phases=value["damage_reduction_phases"],
            damage_bonus_percent=value["damage_bonus_percent"],
            damage_bonus_phases=value["damage_bonus_phases"],
            trap_phases=value.get("trap_phases", 0),
        )

    result = payload.get("match_result")
    return MatchState(
        player=combatant_from_dict(payload["player"]),
        opponent=combatant_from_dict(payload["opponent"]),
        elapsed_seconds=payload["elapsed_seconds"],
        phase_index=payload["phase_index"],
        finished=payload["finished"],
        match_result=ArenaMatchResult(result) if result else None,
        winner=payload.get("winner"),
    )
