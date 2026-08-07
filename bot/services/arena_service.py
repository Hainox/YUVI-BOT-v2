from __future__ import annotations

from datetime import datetime
from datetime import timedelta
from datetime import timezone
import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

# Lazy import keeps pure lobby tests independent from bot runtime settings.
class _EconomyServiceProxy:
    async def debit(self, *args, **kwargs):
        from bot.services import economy_service as runtime_economy_service

        return await runtime_economy_service.debit(*args, **kwargs)

    async def credit(self, *args, **kwargs):
        from bot.services import economy_service as runtime_economy_service

        return await runtime_economy_service.credit(*args, **kwargs)

    def __getattr__(self, name: str):
        from bot.services import economy_service as runtime_economy_service

        return getattr(runtime_economy_service, name)


economy_service = _EconomyServiceProxy()


def _economy_service():
    return economy_service
from common.arena.config import ArenaConfig
from common.arena.schemas import FighterType
from common.models.arena import ArenaMatch


class ArenaServiceError(Exception):
    """Base error for Arena lobby operations."""


class InvalidMatch(ArenaServiceError):
    """Invalid amount, fighter or match input."""


class MatchNotFound(ArenaServiceError):
    """Match does not exist in the requested chat."""


class MatchConflict(ArenaServiceError):
    """Operation conflicts with the current match lifecycle."""


class DuplicateRequest(ArenaServiceError):
    """A state-changing request was already applied."""


_CONFIG = ArenaConfig()
logger = logging.getLogger(__name__)


def _now(value: datetime | None) -> datetime:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        return current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc)


def _validate_fighter(fighter: FighterType | str) -> str:
    try:
        return FighterType(fighter).value
    except (TypeError, ValueError) as exc:
        raise InvalidMatch("Неизвестный боец") from exc


def _validate_bet(bet: int) -> None:
    if isinstance(bet, bool) or not isinstance(bet, int):
        raise InvalidMatch("Ставка должна быть целым числом")
    if bet < _CONFIG.min_bet:
        raise InvalidMatch(f"Минимальная ставка — {_CONFIG.min_bet} ювиков")


async def _get_match_for_update(
    session: AsyncSession, chat_id: int, match_id: int
) -> ArenaMatch:
    match = (
        await session.execute(
            select(ArenaMatch)
            .where(ArenaMatch.chat_id == chat_id, ArenaMatch.id == match_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if match is None:
        raise MatchNotFound(f"Матч #{match_id} не найден")
    return match


def _refund_key(match_id: int, side: str) -> str:
    return f"arena:match:{match_id}:{side}:refund"


async def create_match(
    session: AsyncSession,
    chat_id: int,
    creator_id: int,
    fighter: FighterType | str,
    bet: int,
    idempotency_key: str,
    *,
    now: datetime | None = None,
) -> ArenaMatch:
    if not idempotency_key or len(idempotency_key) > 120:
        raise InvalidMatch("Некорректный ключ идемпотентности")
    _validate_bet(bet)
    fighter_value = _validate_fighter(fighter)

    creator_ref = f"arena:match:{idempotency_key}:creator"
    try:
        debited = await _economy_service().debit(
            session,
            chat_id,
            creator_id,
            bet,
            kind="arena_match_creator_bet",
            ref_id=creator_ref,
        )
        if not debited:
            raise DuplicateRequest("Запрос создания матча уже обработан")

        created_at = _now(now)
        match = ArenaMatch(
            chat_id=chat_id,
            creator_id=creator_id,
            creator_fighter=fighter_value,
            creator_bet=bet,
            creation_idempotency_key=idempotency_key,
            status="waiting",
            settlement_status="pending",
            created_at=created_at,
            expires_at=created_at + timedelta(seconds=_CONFIG.open_match_ttl_seconds),
        )
        session.add(match)
        await session.commit()
        return match
    except Exception:
        await session.rollback()
        raise


async def list_open_matches(
    session: AsyncSession, chat_id: int, *, limit: int = 50
) -> list[ArenaMatch]:
    if not 1 <= limit <= 100:
        raise InvalidMatch("limit должен быть от 1 до 100")
    result = await session.execute(
        select(ArenaMatch)
        .where(ArenaMatch.chat_id == chat_id, ArenaMatch.status == "waiting")
        .order_by(ArenaMatch.created_at.desc())
        .limit(limit)
    )
    return list(result.scalars().all())


async def list_user_matches(
    session: AsyncSession, chat_id: int, user_id: int, *, limit: int = 20
) -> list[ArenaMatch]:
    """Return the viewer's own lifecycle matches for lobby recovery."""
    if not 1 <= limit <= 100:
        raise InvalidMatch("limit должен быть от 1 до 100")
    result = await session.execute(
        select(ArenaMatch)
        .where(
            ArenaMatch.chat_id == chat_id,
            (ArenaMatch.creator_id == user_id) | (ArenaMatch.opponent_id == user_id),
            ArenaMatch.status.in_(["waiting", "accepting", "active"]),
        )
        .order_by(ArenaMatch.created_at.desc())
        .limit(limit)
    )
    return list(result.scalars().all())


async def accept_match(
    session: AsyncSession,
    chat_id: int,
    match_id: int,
    opponent_id: int,
    fighter: FighterType | str,
    bet: int,
    idempotency_key: str,
    *,
    now: datetime | None = None,
) -> ArenaMatch:
    _validate_bet(bet)
    fighter_value = _validate_fighter(fighter)
    match = await _get_match_for_update(session, chat_id, match_id)
    current_time = _now(now)

    if match.status != "waiting":
        raise MatchConflict("Матч уже принят или завершён")
    if current_time >= _now(match.expires_at):
        raise MatchConflict("Срок действия матча истёк")
    if match.creator_id == opponent_id:
        raise MatchConflict("Нельзя принять матч самого себя")
    if not idempotency_key or len(idempotency_key) > 120:
        raise InvalidMatch("Некорректный ключ идемпотентности")

    opponent_ref = f"arena:match:{match_id}:opponent:{idempotency_key}"
    try:
        debited = await _economy_service().debit(
            session,
            chat_id,
            opponent_id,
            bet,
            kind="arena_match_opponent_bet",
            ref_id=opponent_ref,
        )
        if not debited:
            raise DuplicateRequest("Запрос принятия матча уже обработан")

        match.opponent_id = opponent_id
        match.opponent_fighter = fighter_value
        match.opponent_bet = bet
        match.status = "accepting"
        match.accept_deadline = current_time + timedelta(seconds=_CONFIG.accept_confirmation_seconds)
        await session.commit()
        return match
    except Exception:
        await session.rollback()
        raise


async def confirm_match(
    session: AsyncSession, chat_id: int, match_id: int, user_id: int, *, now: datetime | None = None
) -> ArenaMatch:
    match = await _get_match_for_update(session, chat_id, match_id)
    current_time = _now(now)
    if match.status == "active":
        if user_id not in {match.creator_id, match.opponent_id}:
            raise MatchConflict("Вы не участник этого матча")
        await session.commit()
        return match
    if match.status != "accepting":
        raise MatchConflict("Матч не ожидает подтверждения")
    if match.accept_deadline is not None and current_time >= _now(match.accept_deadline):
        raise MatchConflict("Срок подтверждения истёк")
    if user_id == match.creator_id:
        match.creator_confirmed = True
    elif user_id == match.opponent_id:
        match.opponent_confirmed = True
    else:
        raise MatchConflict("Вы не участник этого матча")

    if match.creator_confirmed and match.opponent_confirmed:
        match.status = "active"
        match.started_at = current_time
    await session.commit()
    return match


async def cancel_match(
    session: AsyncSession, chat_id: int, match_id: int, user_id: int
) -> ArenaMatch:
    match = await _get_match_for_update(session, chat_id, match_id)
    if match.status == "cancelled":
        await session.commit()
        return match
    if match.status != "waiting":
        raise MatchConflict("Отменить можно только открытый матч")
    if match.creator_id != user_id:
        raise MatchConflict("Отменить матч может только создатель")

    await _refund_side(session, match, match.creator_id, match.creator_bet, "creator")
    match.status = "cancelled"
    match.settlement_status = "refunded"
    match.resolved_at = datetime.now(timezone.utc)
    await session.commit()
    return match


async def expire_match(
    session: AsyncSession,
    chat_id: int,
    match_id: int,
    *,
    now: datetime | None = None,
) -> ArenaMatch:
    match = await _get_match_for_update(session, chat_id, match_id)
    current_time = _now(now)
    if match.status in {"cancelled", "refunded"}:
        await session.commit()
        return match
    if match.status not in {"waiting", "accepting"}:
        raise MatchConflict("Матч уже завершён")
    deadline = match.expires_at if match.status == "waiting" else match.accept_deadline
    if deadline is not None and current_time < _now(deadline):
        raise MatchConflict("Срок матча ещё не истёк")

    await _refund_side(session, match, match.creator_id, match.creator_bet, "creator")
    if match.opponent_id is not None and match.opponent_bet is not None:
        await _refund_side(session, match, match.opponent_id, match.opponent_bet, "opponent")
    match.status = "refunded"
    match.settlement_status = "refunded"
    match.resolved_at = current_time
    await session.commit()
    return match


async def expire_due_matches(
    session: AsyncSession,
    chat_id: int | None = None,
    *,
    now: datetime | None = None,
    limit: int = 100,
) -> list[ArenaMatch]:
    """Expire waiting/accepting matches whose current lifecycle deadline passed.

    A scheduler can call this function periodically. Each match is re-read with
    ``FOR UPDATE`` by ``expire_match`` before money moves, so a concurrent
    accept/confirm worker wins or loses atomically at the row level.
    """
    if not 1 <= limit <= 500:
        raise InvalidMatch("limit должен быть от 1 до 500")
    current_time = _now(now)
    conditions = [ArenaMatch.status == "waiting"]
    # Accepting matches use their shorter confirmation deadline; the service
    # filters them in Python because the two lifecycle deadlines differ.
    result = await session.execute(
        select(ArenaMatch)
        .where(
            *(conditions + ([ArenaMatch.chat_id == chat_id] if chat_id is not None else []))
        )
        .with_for_update(skip_locked=True)
        .limit(limit)
    )
    candidates = list(result.scalars().all())
    accepting_result = await session.execute(
        select(ArenaMatch)
        .where(
            ArenaMatch.status == "accepting",
            *([ArenaMatch.chat_id == chat_id] if chat_id is not None else []),
        )
        .with_for_update(skip_locked=True)
        .limit(limit)
    )
    candidates.extend(accepting_result.scalars().all())

    expired: list[ArenaMatch] = []
    for candidate in candidates:
        deadline = (
            candidate.expires_at
            if candidate.status == "waiting"
            else candidate.accept_deadline
        )
        if deadline is None or current_time < _now(deadline):
            continue
        try:
            expired.append(await expire_match(session, candidate.chat_id, candidate.id, now=current_time))
        except MatchConflict:
            # Another worker may have accepted or settled the row after the
            # candidate scan; it is safe to skip it on this scheduler tick.
            continue
    return expired


_ARENA_EXPIRY_JOB_ID = "arena_match_expiry"


def register_expiry_job(scheduler, *, interval_seconds: int = 15) -> None:
    """Register the money-safety expiry sweep in the shared bot scheduler."""
    from common.db.session import SessionLocal

    async def _job() -> None:
        async with SessionLocal() as session:
            try:
                expired = await expire_due_matches(session, limit=100)
                if expired:
                    logger.info("arena expiry: refunded matches=%s", len(expired))
            except Exception:  # noqa: BLE001 - a scheduler tick must not stop future ticks
                await session.rollback()
                logger.exception("arena expiry tick failed")

    scheduler.add_job(
        _job,
        "interval",
        seconds=interval_seconds,
        coalesce=True,
        max_instances=1,
        misfire_grace_time=30,
        id=_ARENA_EXPIRY_JOB_ID,
        replace_existing=True,
    )


async def _refund_side(
    session: AsyncSession,
    match: ArenaMatch,
    user_id: int,
    amount: int,
    side: str,
) -> None:
    refunded = await _economy_service().credit(
        session,
        match.chat_id,
        user_id,
        amount,
        kind="arena_match_refund",
        ref_id=_refund_key(match.id, side),
    )
    if not refunded:
        # A prior successful refund is safe to replay; the locked match row
        # prevents two workers from trying to settle the same lifecycle.
        return
