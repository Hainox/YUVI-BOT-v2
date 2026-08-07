from __future__ import annotations

from datetime import datetime
from datetime import timedelta
from datetime import timezone
from decimal import Decimal
from decimal import ROUND_HALF_UP
import logging

from sqlalchemy import insert
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from common.arena.config import ArenaConfig
from common.arena.schemas import ArenaMatchResult
from common.arena.schemas import FighterType
from common.models.arena import ArenaFighterProgress
from common.models.arena import ArenaFund
from common.models.arena import ArenaFundLedger
from common.models.arena import ArenaMatch
from common.models.arena import ArenaProfile


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


# --- Settlement (Phase 5, FIGHTING_SPEC.md §6/§8) -----------------------
#
# `ArenaSessionService` (arena_session_service.py) deliberately owns no money
# and no database transaction — its own docstring says "Phase 5 settlement
# consumes the terminal snapshot produced here". Nothing in the repo ever
# called that consumer: a match that reaches a decisive combat outcome (or a
# disconnect/forfeit) had both stakes escrowed via `debit()` at create/accept
# time, but no code path ever paid the winner, refunded a draw, or even
# marked `ArenaMatch.status = "finished"` — the row just stays "active"
# forever and the runtime worker re-ticks it every second, no-op, while the
# Redis snapshot silently expires after its 2h TTL. `settle_match` below is
# that missing consumer; `arena_runtime_worker.py` calls it right after any
# `tick()` observes `state["terminal"]`.


def _xp_threshold(level: int) -> int:
    """FIGHTING_SPEC.md §8.2: XP до следующего уровня = 500 + (level-1)*100."""
    return 500 + (level - 1) * 100


async def _get_or_create_profile_for_update(session: AsyncSession, user_id: int) -> ArenaProfile:
    """Get-or-create + lock a global (not chat-scoped) ArenaProfile row —
    same insert-on-conflict-do-nothing-then-select-for-update idiom as
    `economy_service._get_or_create_balance`."""
    stmt = (
        pg_insert(ArenaProfile)
        .values(user_id=user_id)
        .on_conflict_do_nothing(index_elements=["user_id"])
    )
    await session.execute(stmt)
    return (
        await session.execute(
            select(ArenaProfile).where(ArenaProfile.user_id == user_id).with_for_update()
        )
    ).scalar_one()


async def _get_or_create_fighter_progress_for_update(
    session: AsyncSession, profile_id: int, fighter_type: str
) -> ArenaFighterProgress:
    stmt = (
        pg_insert(ArenaFighterProgress)
        .values(profile_id=profile_id, fighter_type=fighter_type)
        .on_conflict_do_nothing(
            index_elements=["profile_id", "fighter_type"]
        )
    )
    await session.execute(stmt)
    return (
        await session.execute(
            select(ArenaFighterProgress).where(
                ArenaFighterProgress.profile_id == profile_id,
                ArenaFighterProgress.fighter_type == fighter_type,
            ).with_for_update()
        )
    ).scalar_one()


async def _grant_fighter_xp(
    session: AsyncSession, profile: ArenaProfile, fighter_type: str, xp_gained: int
) -> None:
    if xp_gained <= 0:
        return
    progress = await _get_or_create_fighter_progress_for_update(session, profile.id, fighter_type)
    progress.xp += xp_gained
    while progress.level < _CONFIG.max_fighter_level:
        threshold = _xp_threshold(progress.level)
        if progress.xp < threshold:
            break
        progress.xp -= threshold
        progress.level += 1


async def _credit_arena_fund(
    session: AsyncSession,
    chat_id: int,
    amount: int,
    *,
    kind: str,
    idempotency_key: str,
    match_id: int | None = None,
) -> bool:
    """Idempotent credit into the per-chat ArenaFund + append-only
    ArenaFundLedger audit row (FIGHTING_SPEC.md §12 admin/audit requirement).
    Mirrors `economy_service.credit_bank`'s single-statement upsert shape —
    ArenaFund is Arena's own fee destination, separate from the shared
    `chat_bank` (spec §6.2: fee splits 2.5%/2.5% between the two)."""
    try:
        async with session.begin_nested():
            stmt = (
                pg_insert(ArenaFund)
                .values(chat_id=chat_id, balance=amount)
                .on_conflict_do_update(
                    index_elements=["chat_id"], set_={"balance": ArenaFund.balance + amount}
                )
                .returning(ArenaFund.balance)
            )
            new_balance = (await session.execute(stmt)).scalar_one()
            await session.execute(
                insert(ArenaFundLedger).values(
                    chat_id=chat_id,
                    amount=amount,
                    balance_after=new_balance,
                    kind=kind,
                    idempotency_key=idempotency_key,
                    match_id=match_id,
                )
            )
    except IntegrityError:
        logger.info(
            "_credit_arena_fund: idempotency_key=%s (kind=%s) уже применён, пропускаем",
            idempotency_key,
            kind,
        )
        return False
    return True


async def _record_match_stats(session: AsyncSession, match: ArenaMatch, *, result: str) -> None:
    """Updates ArenaProfile (rating + counters) and ArenaFighterProgress
    (XP/level) for both sides of a decisively-settled ("win"/"technical_loss")
    or drawn match. Never called for the both-disconnected refund path — the
    spec attaches no rating/XP consequence to a match that never really
    happened (same treatment as a pre-active `cancel_match`/`expire_match`,
    neither of which touch ArenaProfile either)."""
    creator_id, opponent_id = match.creator_id, match.opponent_id
    # Consistent lock order (ascending user_id) — Pattern 1 (rows locked in a
    # fixed global order) — so two matches settling concurrently that happen
    # to share a player can't deadlock against each other.
    first_id, second_id = sorted((creator_id, opponent_id))
    profiles = {
        first_id: await _get_or_create_profile_for_update(session, first_id),
        second_id: await _get_or_create_profile_for_update(session, second_id),
    }
    creator_profile, opponent_profile = profiles[creator_id], profiles[opponent_id]

    if result == "draw":
        for profile, fighter_type in (
            (creator_profile, match.creator_fighter),
            (opponent_profile, match.opponent_fighter),
        ):
            profile.total_matches += 1
            profile.draws += 1
            await _grant_fighter_xp(session, profile, fighter_type, _CONFIG.base_match_xp)
        return

    winner_profile = profiles[match.winner_id]
    loser_profile = profiles[match.loser_id]
    winner_fighter = (
        match.creator_fighter if match.winner_id == creator_id else match.opponent_fighter
    )
    loser_fighter = (
        match.creator_fighter if match.loser_id == creator_id else match.opponent_fighter
    )

    winner_profile.total_matches += 1
    winner_profile.wins += 1
    winner_profile.rating += _CONFIG.win_rating_delta
    if result == "technical_loss":
        winner_profile.technical_wins += 1
    if match.result_reason == "knockout":
        winner_profile.knockouts += 1
    await _grant_fighter_xp(
        session, winner_profile, winner_fighter, _CONFIG.base_match_xp + _CONFIG.win_bonus_xp
    )

    loser_profile.total_matches += 1
    loser_xp = 0
    if result == "technical_loss":
        loser_profile.technical_losses += 1
        loser_profile.rating = max(0, loser_profile.rating - _CONFIG.technical_loss_rating_delta)
    else:
        loser_profile.losses += 1
        loser_profile.rating = max(0, loser_profile.rating - _CONFIG.loss_rating_delta)
        loser_xp = _CONFIG.base_match_xp
    await _grant_fighter_xp(session, loser_profile, loser_fighter, loser_xp)


async def settle_match(
    session: AsyncSession,
    chat_id: int,
    match_id: int,
    runtime_state: dict,
    *,
    now: datetime | None = None,
) -> ArenaMatch:
    """Atomically settles one terminal Arena match — the Phase 5 consumer
    `ArenaSessionService`'s docstring points to (see module-level comment
    above). `runtime_state` is the terminal Redis snapshot the caller already
    holds (`ArenaSessionService.tick`/`submit_action`/`forfeit`'s return
    value) — this function never talks to Redis itself, preserving that
    service's "owns no money" boundary.

    Three branches, per FIGHTING_SPEC.md §6.2/§7/§8:
    - `refund_required` (both players disconnected): full refund to both
      sides, no fee, no rating/XP change.
    - `result == "draw"`: full refund to both sides, no fee (spec §6.2), but
      both sides still get draw XP (spec §8.2 lists "PvP-ничья: 100 XP").
    - `result in ("win", "technical_loss")`: winner gets
      `pot - 5% fee` (fee split 2.5%/2.5% between chat_bank and the
      per-chat ArenaFund), loser gets nothing; both sides' rating/XP update
      per §8.

    Idempotent: `settlement_status != "pending"` is a same-row no-op
    returning the already-settled match (roadmap Phase 5 requirement
    "повторный settle возвращает сохранённый результат") — safe to call
    repeatedly from a 1-second tick loop that may observe the same terminal
    match more than once before this function's own commit lands.

    No internal try/except-rollback here — same shape as `accept_duel`/
    `accept_autobattle` (which also do several money-moving calls before a
    single final commit): on an unexpected mid-function failure, the
    exception propagates raw and the CALLER's session lifecycle decides what
    happens to the uncommitted work (a real `SessionLocal()` block discards
    it on close; wrapping here would additionally roll back whatever the
    caller had already committed earlier in the same test/request
    transaction, which is a documented false failure mode this repo hit
    before — see test_duel_service.py's
    test_accept_duel_payout_failure_after_rng_does_not_fabricate_or_double_charge
    docstring for the full explanation).
    """
    match = await _get_match_for_update(session, chat_id, match_id)
    if match.settlement_status != "pending" or match.status != "active":
        await session.commit()
        return match

    current_time = _now(now)
    idempotency_key = f"arena:match:{match_id}:settle"

    if runtime_state.get("refund_required"):
        await _refund_side(session, match, match.creator_id, match.creator_bet, "creator")
        if match.opponent_id is not None and match.opponent_bet is not None:
            await _refund_side(session, match, match.opponent_id, match.opponent_bet, "opponent")
        match.status = "finished"
        match.settlement_status = "refunded"
        match.result_reason = runtime_state.get("reason")
        match.resolved_at = current_time
        match.finished_at = current_time
        await session.commit()
        return match

    result = runtime_state.get("result")
    winner_id = runtime_state.get("winner_id")
    loser_id = runtime_state.get("loser_id")
    reason = runtime_state.get("reason")

    if result == ArenaMatchResult.DRAW.value:
        await _refund_side(session, match, match.creator_id, match.creator_bet, "creator")
        await _refund_side(session, match, match.opponent_id, match.opponent_bet, "opponent")
        match.match_result = "draw"
        match.result_reason = reason
        match.status = "finished"
        match.settlement_status = "refunded"
        match.resolved_at = current_time
        match.finished_at = current_time
        await _record_match_stats(session, match, result="draw")
        await session.commit()
        return match

    if (
        result in (ArenaMatchResult.WIN.value, ArenaMatchResult.TECHNICAL_LOSS.value)
        and winner_id is not None
        and loser_id is not None
    ):
        pot = match.creator_bet + (match.opponent_bet or 0)
        fee_total = int(
            (Decimal(pot) * _CONFIG.fee_percent / Decimal(100)).quantize(
                Decimal("1"), rounding=ROUND_HALF_UP
            )
        )
        fee_fund = fee_total // 2
        fee_chat = fee_total - fee_fund
        payout = pot - fee_total

        await _economy_service().credit(
            session, chat_id, winner_id, payout, kind="arena_match_payout", ref_id=idempotency_key
        )
        await _economy_service().credit_bank(
            session, chat_id, fee_chat, kind="arena_match_fee", ref_id=f"{idempotency_key}:chat_fee"
        )
        await _credit_arena_fund(
            session,
            chat_id,
            fee_fund,
            kind="arena_match_fee",
            idempotency_key=f"{idempotency_key}:fund_fee",
            match_id=match.id,
        )

        match.match_result = result
        match.winner_id = winner_id
        match.loser_id = loser_id
        match.result_reason = reason
        match.status = "finished"
        match.settlement_status = "paid"
        match.settlement_idempotency_key = idempotency_key
        match.payout_amount = payout
        match.fee_chat = fee_chat
        match.fee_fund = fee_fund
        match.resolved_at = current_time
        match.finished_at = current_time
        await _record_match_stats(session, match, result=result)
        await session.commit()
        return match

    # Terminal Redis state that matches none of the three documented
    # shapes ArenaSessionService ever produces — fail loudly rather than
    # guess at a money-moving default (T-04.1-style discipline: never
    # fabricate an outcome).
    raise ArenaServiceError(
        f"Матч #{match_id}: нераспознанное терминальное состояние "
        f"(result={result!r}, refund_required={runtime_state.get('refund_required')!r})"
    )
