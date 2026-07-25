"""GET /api/v1/quests (QUEST-01/02) — тонкий роут над `quests_service` и
`achievements_service`. `user_id`/`chat_id` берутся ТОЛЬКО из `AuthContext`
(`require_membership`) — тот же IDOR-контракт, что и `api/routes/farm.py`.

То, что GET здесь выполняет claim (мутацию/побочный эффект — `claim_quests`/
`claim_achievements` коммитят изменения на каждый вызов), — не новый паттерн:
`GET /api/v1/farm` уже вызывает `clicker_service.get_farm_state`, который
тоже мутирует и коммитит на каждое чтение (офлайн-начисление CP). Здесь то
же самое поведение, распространённое на квесты/достижения.
"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi import Depends

from api.deps import AuthContext
from api.deps import require_membership
from bot.config import settings
from bot.services import achievements_service
from bot.services import clicker_service
from bot.services import quests_service
from common.db.session import SessionLocal

router = APIRouter()


@router.get("/api/v1/quests")
async def get_quests(auth: AuthContext = Depends(require_membership)) -> dict:
    async with SessionLocal() as session:
        quest_result = await quests_service.claim_quests(session, auth.chat_id, auth.user_id)
        achievement_result = await achievements_service.claim_achievements(
            session, auth.chat_id, auth.user_id
        )

        quest_status = await quests_service.get_quest_status(session, auth.chat_id, auth.user_id)
        achievement_status = await achievements_service.get_achievement_status(
            session, auth.chat_id, auth.user_id
        )
        total_completions = await quests_service.get_total_completions(
            session, auth.chat_id, auth.user_id
        )
        achievement_bonus_levels = await achievements_service.get_total_bonus_levels(
            session, auth.chat_id, auth.user_id
        )
        effective_max_level = await clicker_service.get_effective_max_level(
            session, auth.chat_id, auth.user_id
        )

        return {
            "quests": quest_status,
            "achievements": achievement_status,
            "base_max_level": settings.farm_max_level,
            "quest_bonus_levels": total_completions // clicker_service.QUEST_COMPLETIONS_PER_BONUS_LEVEL,
            "achievement_bonus_levels": achievement_bonus_levels,
            "effective_max_level": effective_max_level,
            "newly_claimed_quests": quest_result["claimed"],
            "newly_unlocked_achievements": achievement_result["unlocked"],
        }
