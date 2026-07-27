"""/quests (QUEST-01/02) — ежедневные квесты + постоянные ачивки,
разблокирующие бонусные уровни фермы.

Pull-claim на КАЖДЫЙ вызов, планировщика нет — та же причина, что у
`bot/services/awards_service.py`: детерминированная проверка прогресса +
идемпотентный INSERT-gate (`on_conflict_do_nothing`) в
`quests_service.claim_quests`/`achievements_service.claim_achievements`
делают повторный вызов безопасным no-op (награда не выплачивается дважды).

Тонкий хендлер с единственным `try/except Exception` ->
`logger.exception` + ответ пользователю — домашний стандарт для всех
сервис-завязанных команд, см. `bot/handlers/ai_ask.py` и
`bot/handlers/ai_card.py`.
"""

from __future__ import annotations

import logging

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import settings
from bot.services import achievements_service
from bot.services import clicker_service
from bot.services import quests_service

router = Router()
logger = logging.getLogger(__name__)


def _format_quests_post(
    quest_status: list[dict],
    achievement_status: list[dict],
    effective_cap: int,
    base_cap: int,
) -> str:
    lines = ["Квесты дня"]
    for q in quest_status:
        mark = "[x]" if q["claimed"] else ("[!]" if q["done"] else "[ ]")
        lines.append(f"{mark} {q['label']} — {q['reward']} ювиков")
        lines.append(f"    {q['description']}")

    lines.append("")
    lines.append("Достижения")
    for a in achievement_status:
        mark = "[открыто]" if a["unlocked"] else "[закрыто]"
        lines.append(f"{mark} {a['label']} — +{a['bonus_levels']} ур. фермы, {a['reward']} ювиков")
        lines.append(f"    {a['description']}")

    lines.append("")
    lines.append(f"Ферма: доступно до уровня {effective_cap} из 99 (база {base_cap} + квесты/ачивки)")
    return "\n".join(lines)


@router.message(Command("quests"))
async def quests_command(message: Message, session: AsyncSession) -> None:
    if message.from_user is None:
        return

    chat_id = message.chat.id
    user_id = message.from_user.id

    try:
        await quests_service.claim_quests(session, chat_id, user_id)
        await achievements_service.claim_achievements(session, chat_id, user_id)
        quest_status = await quests_service.get_quest_status(session, chat_id, user_id)
        achievement_status = await achievements_service.get_achievement_status(session, chat_id, user_id)
        effective_cap = await clicker_service.get_effective_max_level(session, chat_id, user_id)
    except Exception:  # noqa: BLE001 - хендлер обязан сообщить об ошибке в чат, а не упасть молча
        logger.exception("quests_command: упал для chat_id=%s user_id=%s", chat_id, user_id)
        await message.reply("Не получилось показать квесты, попробуй ещё раз позже.")
        return

    text = _format_quests_post(quest_status, achievement_status, effective_cap, settings.farm_max_level)
    await message.reply(text)
