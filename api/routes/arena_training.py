"""Arena training-mode API (Phase 6, FIGHTING_SPEC.md §9).

Thin routes over `bot.services.arena_training_service.ArenaTrainingService` —
same auth/error-mapping shape as `api/routes/arena_runtime.py`, but simpler:
no `ArenaMatch` row to load/authorize against (training has no opponent user,
no money, so `(auth.chat_id, auth.user_id)` alone identifies the session —
identity comes ONLY from `AuthContext`, never the request body, same IDOR
contract as every other route in this repo).
"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Request
from pydantic import BaseModel

from api.deps import AuthContext
from api.deps import require_membership
from bot.services import arena_training_service

router = APIRouter()


class StartTrainingBody(BaseModel):
    fighter: str
    opponent_fighter: str | None = None


class TrainingActionBody(BaseModel):
    action: str


class SetPausedBody(BaseModel):
    paused: bool


def _service(request: Request) -> arena_training_service.ArenaTrainingService:
    redis_client = getattr(request.app.state, "redis", None)
    if redis_client is None:
        raise HTTPException(status_code=503, detail="Тренировка недоступна")
    return arena_training_service.ArenaTrainingService(redis_client)


def _map_error(exc: arena_training_service.ArenaTrainingError) -> HTTPException:
    if isinstance(exc, arena_training_service.TrainingSessionNotFound):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, arena_training_service.TrainingActionNotAvailable):
        return HTTPException(status_code=422, detail=str(exc))
    if isinstance(exc, arena_training_service.TrainingSessionPaused):
        return HTTPException(status_code=409, detail=str(exc))
    return HTTPException(status_code=400, detail=str(exc))


@router.post("/api/v1/arena/training")
async def post_start_training(
    body: StartTrainingBody, request: Request, auth: AuthContext = Depends(require_membership)
) -> dict:
    service = _service(request)
    try:
        return await service.start_or_restart(
            auth.chat_id, auth.user_id, body.fighter, body.opponent_fighter
        )
    except arena_training_service.ArenaTrainingError as exc:
        raise _map_error(exc) from exc


@router.get("/api/v1/arena/training")
async def get_training_state(
    request: Request, auth: AuthContext = Depends(require_membership)
) -> dict:
    service = _service(request)
    try:
        return await service.get_state(auth.chat_id, auth.user_id)
    except arena_training_service.ArenaTrainingError as exc:
        raise _map_error(exc) from exc


@router.post("/api/v1/arena/training/actions")
async def post_training_action(
    body: TrainingActionBody, request: Request, auth: AuthContext = Depends(require_membership)
) -> dict:
    service = _service(request)
    try:
        return await service.submit_action(auth.chat_id, auth.user_id, body.action)
    except arena_training_service.ArenaTrainingError as exc:
        raise _map_error(exc) from exc


@router.post("/api/v1/arena/training/pause")
async def post_training_pause(
    body: SetPausedBody, request: Request, auth: AuthContext = Depends(require_membership)
) -> dict:
    service = _service(request)
    try:
        return await service.set_paused(auth.chat_id, auth.user_id, body.paused)
    except arena_training_service.ArenaTrainingError as exc:
        raise _map_error(exc) from exc
