"""GET /api/v1/admin/summary + GET /api/v1/admin/analytics (CASINO-03, D-03).

Оба роута гейтятся ИСКЛЮЧИТЕЛЬНО `require_admin` (живой `getChatMember`,
НЕ `BOT_ADMIN_IDS`) — T-04.3-05, единственный по-настоящему важный контроль
безопасности этой фазы. Клиентский гейт плитки «Админ» в хабе — только
косметика (Pitfall 4), сервер независимо перепроверяет права на каждом
запросе.

`summary` — тонкая обёртка над уже существующим `economy_service.
get_chat_summary` (банк чата, оборот, число открытых рынков — без
дублирования логики). `analytics` компонует три read-only агрегации
`admin_analytics_service` (популярность игр, оборот ювиков, DAU) за период,
выбранный query-параметром `period` ("24h"/"7d"/"30d", default "7d").
"""

from __future__ import annotations

from datetime import datetime
from datetime import timedelta

from fastapi import APIRouter
from fastapi import Depends

from api.deps import AuthContext
from api.deps import require_admin
from bot.services import admin_analytics_service
from bot.services import economy_service
from common.db.session import SessionLocal

router = APIRouter()

_PERIOD_TO_DELTA: dict[str, timedelta] = {
    "24h": timedelta(hours=24),
    "7d": timedelta(days=7),
    "30d": timedelta(days=30),
}
_DEFAULT_PERIOD = "7d"


def _period_to_since(period: str) -> datetime:
    """Маппит "24h"/"7d"/"30d" в datetime (utcnow − delta); неизвестное
    значение молча трактуется как default "7d" (не 400 — read-only виджет,
    не стоит ронять экран из-за опечатки в query)."""
    delta = _PERIOD_TO_DELTA.get(period, _PERIOD_TO_DELTA[_DEFAULT_PERIOD])
    return datetime.utcnow() - delta


@router.get("/api/v1/admin/summary")
async def get_admin_summary(auth: AuthContext = Depends(require_admin)) -> dict:
    async with SessionLocal() as session:
        return await economy_service.get_chat_summary(session, auth.chat_id)


@router.get("/api/v1/admin/analytics")
async def get_admin_analytics(
    period: str = _DEFAULT_PERIOD, auth: AuthContext = Depends(require_admin)
) -> dict:
    since = _period_to_since(period)
    async with SessionLocal() as session:
        return {
            "game_popularity": await admin_analytics_service.get_game_popularity(
                session, auth.chat_id, since
            ),
            "turnover": await admin_analytics_service.get_turnover(session, auth.chat_id, since),
            "active_players": await admin_analytics_service.get_active_players(
                session, auth.chat_id, since
            ),
        }
