from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import redis.asyncio as redis
from fastapi import FastAPI
from fastapi import Request
from fastapi.responses import JSONResponse

from api.deps import InvalidInitData
from api.routes import events
from bot.config import settings


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Singleton httpx/redis-клиенты на app.state — создаются один раз при
    старте процесса, живут всё время его жизни (Context7 /encode/httpx: не
    создавать AsyncClient на каждый запрос; тот же идиом, что
    bot/services/scheduler.py::get_scheduler()).

    Redis — best-effort (D-02): если REDIS_URL не задан, app.state.redis
    остаётся None, а отсутствие Redis не должно ронять api (только
    деградирует live-UI, деньги идут через economy_service независимо).
    """
    app.state.http_client = httpx.AsyncClient(timeout=10.0)
    app.state.redis = redis.from_url(settings.redis_url) if settings.redis_url else None

    yield

    await app.state.http_client.aclose()
    if app.state.redis is not None:
        await app.state.redis.aclose()


app = FastAPI(title="Yuvi Bot v2 API", version="0.1.0", lifespan=lifespan)
app.include_router(events.router)


@app.exception_handler(InvalidInitData)
async def handle_invalid_init_data(request: Request, exc: InvalidInitData) -> JSONResponse:
    """Маппит InvalidInitData -> 401 без утечки текста исключения клиенту
    (Information Disclosure) — см. api/deps.py::validate_init_data (D-01)."""
    return JSONResponse(status_code=401, content={"detail": "invalid init data"})


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "service": "api"}

