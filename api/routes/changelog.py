"""GET /api/v1/changelog — тонкий роут над `bot.services.changelog_service`
(WHATSNEW-01), лента «Что нового» Mini App. Read-only: публикация — только
через `/post_update` (`bot/handlers/owner.py`, `settings.owner_id`), Mini App
эту ленту не пишет. Auth — обычный `require_membership` (та же авторизация,
что у остальных read-путей Mini App), лента сама по себе не чат-специфична,
но доступ остаётся только участникам чата, как и весь остальной Mini App.
"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi import Depends

from api.deps import require_membership
from bot.services import changelog_service
from common.db.session import SessionLocal

router = APIRouter()


@router.get("/api/v1/changelog")
async def get_changelog(auth=Depends(require_membership)) -> dict:
    async with SessionLocal() as session:
        entries = await changelog_service.list_entries(session)
        return {
            "entries": [
                {
                    "id": entry.id,
                    "title": entry.title,
                    "body": entry.body,
                    "created_at": entry.created_at,
                }
                for entry in entries
            ]
        }
