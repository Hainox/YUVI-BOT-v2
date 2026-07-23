from __future__ import annotations

import importlib
import logging
import pkgutil
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import redis.asyncio as redis
from fastapi import APIRouter
from fastapi import FastAPI
from fastapi import Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from api import routes as routes_package
from api.deps import InvalidInitData
from bot.config import settings


def _discover_routers() -> list[APIRouter]:
    """Импортирует все модули `api.routes` и собирает их атрибуты `router`
    (форма `bot/main.py::_discover_routers`). Детерминированный порядок
    (sorted по имени модуля) — регистрация воспроизводима между запусками.
    Каждый новый `api/routes/*.py` с `router = APIRouter()` подключается
    автоматически, без правки этого файла."""
    routers: list[APIRouter] = []
    module_infos = sorted(
        pkgutil.iter_modules(routes_package.__path__), key=lambda m: m.name
    )
    for module_info in module_infos:
        module = importlib.import_module(f"{routes_package.__name__}.{module_info.name}")
        router = getattr(module, "router", None)
        if isinstance(router, APIRouter):
            routers.append(router)
    return routers


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


logging.basicConfig(level=settings.log_level)
logger = logging.getLogger(__name__)

app = FastAPI(title="Yuvi Bot v2 API", version="0.1.0", lifespan=lifespan)

# Mini App frontend (miniapp/, port 8003) is a different origin than this api
# (port 8002) — browsers block fetch()/EventSource without explicit CORS (WR-06).
# Not a wildcard: X-Telegram-Init-Data is a bearer-like credential.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.mini_app_frontend_origin],
    allow_credentials=True,
    allow_methods=["GET", "POST"],
    allow_headers=["X-Telegram-Init-Data"],
)

for _router in _discover_routers():
    app.include_router(_router)


@app.exception_handler(InvalidInitData)
async def handle_invalid_init_data(request: Request, exc: InvalidInitData) -> JSONResponse:
    """Маппит InvalidInitData -> 401 без утечки текста исключения клиенту
    (Information Disclosure) — см. api/deps.py::validate_init_data (D-01).

    Короткая причина (напр. "hash mismatch", "expired") логируется на
    сервере — это категория ошибки, не сами данные initData, безопасно для
    постоянного логирования и сильно ускоряет диагностику живых 401."""
    logger.info("invalid init data: %s (path=%s)", exc, request.url.path)
    return JSONResponse(status_code=401, content={"detail": "invalid init data"})


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "service": "api"}

