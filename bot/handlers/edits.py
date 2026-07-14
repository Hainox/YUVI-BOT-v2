"""Роутер правок сообщений (D-03, append-only).

Хендлер тонкий: только парсинг события Telegram + один вызов
message_service.save_edit + commit. Оригинал messages.text НИКОГДА не
перезаписывается — правка добавляется отдельной строкой в message_edits.
"""

from __future__ import annotations

from aiogram import Router
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.services import message_service

router = Router()


@router.edited_message()
async def on_edited_message(event: Message, session: AsyncSession) -> None:
    saved = await message_service.save_edit(
        session,
        chat_id=event.chat.id,
        telegram_message_id=event.message_id,
        new_text=event.text or event.caption,
    )
    if saved:
        await session.commit()
