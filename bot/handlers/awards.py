"""`/awards` (AWARDS-01/AWARDS-02) — ежедневный пост с 7 номинациями.

Тонкий хендлер: вся скоринг/денежная логика — в `awards_service.run_awards`
(уже коммитит выплаты и резолвит Steam-игру дня), здесь только вызов
сервиса + `awards_service.format_awards_post` (общий рендер, переиспользует
тот же текст, что и автопост 23:55 МСК — `awards_service.
register_daily_autopost`, D-04 приоритет reuse over duplicate)."""

from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.services import awards_service

router = Router()


@router.message(Command("awards"))
async def awards_command(message: Message, session: AsyncSession) -> None:
    result = await awards_service.run_awards(session, message.chat.id)
    text = await awards_service.format_awards_post(session, result)
    await message.answer(text, parse_mode="HTML")  # всегда отвечаем прямо в группе
