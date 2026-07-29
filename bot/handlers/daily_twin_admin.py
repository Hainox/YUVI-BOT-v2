"""Admin-рубильник фичи "дневной двойник" целиком (TWIN-03, запрошено
2026-07-29 — живой инцидент: персона дня оказалась активным участником
треда, реактивный хендлер отвечал на каждый матчащий апдейт почти без
остановки; владелец бота попросил точечную команду выключить именно эту
фичу целиком, не трогая остальной функционал бота).

Отдельно от /twin_pause /twin_resume (bot/handlers/twin.py) — те дают
ОДНОМУ участнику приостановить СЕБЯ (не быть выбранным/прочитанным), это
персональный opt-out. Команды здесь — глобальный переключатель на ВЕСЬ чат:
выключают/включают разом и проактивный тик (daily_twin_service.
maybe_post_proactively), и реактивные ответы (bot/handlers/daily_twin.py).
Персистентно через bot_settings/settings_service (та же KV, что и
/model_set в ai_admin.py) — переживает рестарт бота, эффект — на следующем
же апдейте/тике, без передеплоя.

Гейт — ChatAdminFilter, тот же live-механизм (bot.get_chat_member через
admin_service.is_chat_admin), что и у /model_set — никакого нового
механизма авторизации не заводится.
"""

from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.filters.chat_admin import ChatAdminFilter
from bot.services import daily_twin_service

router = Router()
router.message.filter(ChatAdminFilter())


@router.message(Command("daily_twin_off"))
async def daily_twin_off_command(message: Message, session: AsyncSession) -> None:
    if message.from_user is None:
        return

    await daily_twin_service.set_enabled(session, message.chat.id, False, message.from_user.id)
    await session.commit()
    await message.answer(
        "Двойник дня выключен целиком: ни проактивных постов, ни ответов на "
        "реплаи/упоминания сегодняшней персоны. Остальные функции бота не "
        "затронуты. Включить обратно — /daily_twin_on."
    )


@router.message(Command("daily_twin_on"))
async def daily_twin_on_command(message: Message, session: AsyncSession) -> None:
    if message.from_user is None:
        return

    await daily_twin_service.set_enabled(session, message.chat.id, True, message.from_user.id)
    await session.commit()
    await message.answer("Двойник дня снова включён.")
