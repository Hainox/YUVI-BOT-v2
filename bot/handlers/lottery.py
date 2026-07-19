"""Ежедневная лотерея `/yuvi` (LOTTERY-01) — «Yuvi_Yuvi дня»: анонс
случайного участника из вчерашних активных, идемпотентно по MSK-дню.

Тонкий хендлер: вся пик-логика — в lottery_service (уже коммитит).
Announcement-only (D-10) — здесь нет ни economy_service, ни tag_service,
ни одного вызова Bot API кроме обычного ответа в чат.
"""

from __future__ import annotations

import html

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.services import lottery_service
from common.models.user import User

router = Router()


async def _display_name(session: AsyncSession, user_id: int) -> str:
    first_name = (
        await session.execute(select(User.first_name).where(User.id == user_id))
    ).scalar_one_or_none()
    return html.escape(first_name or str(user_id))


@router.message(Command("yuvi"))
async def yuvi_command(message: Message, session: AsyncSession) -> None:
    result = await lottery_service.run_lottery(session, message.chat.id)

    if result["winner"] is None:
        await message.answer("Вчера в чате не было активных участников — лотерея пропущена.")
        return

    name = await _display_name(session, result["winner"])

    if not result["is_new"]:
        await message.answer(
            f"🎲 Yuvi_Yuvi дня уже выбран: <b>{name}</b>",
            parse_mode="HTML",
        )
        return

    await message.answer(
        f"🎲 Yuvi_Yuvi дня: <b>{name}</b>",
        parse_mode="HTML",
    )
