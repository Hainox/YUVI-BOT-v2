"""«Жертва дня» (VICTIM-01/02, D-05/D-06/D-09/D-10) — /victim: случайная
жертва получает 228 ювиков из банка чата, реальный Telegram-титул
«Жертва дня» на 24ч (через `tag_service` — единственный владелец
custom_title, этот хендлер сам НИКОГДА не зовёт Bot API напрямую) и
удвоенную комиссию перевода на 24ч (дебафф резолвится в
bot/handlers/economy.py через `victim_service.is_active_victim`).

Тонкий хендлер: вся пик/денежная логика — в victim_service (уже коммитит
приз), Telegram-тег — defensive-эффект ПОСЛЕ этого коммита (форма мута
дуэли: test_duel_accept_handler_survives_mute_failure — деньги уже
двинулись, сбой Telegram API не должен ронять флоу /victim). Broad
`except Exception` вокруг grant_title+commit покрывает не только
Telegram-side сбои, но и DB-уровневые (WR-05, 05-REVIEW.md) — на любой
из них сессия явно откатывается (`session.rollback()`), а не полагается
на неявную подчистку при закрытии сессии.
"""

from __future__ import annotations

import html
import logging

from aiogram import Bot
from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.services import tag_service
from bot.services import victim_service
from common.models.user import User

logger = logging.getLogger(__name__)

router = Router()

TITLE = victim_service.VICTIM_TITLE


async def _display_name(session: AsyncSession, user_id: int) -> str:
    first_name = (
        await session.execute(select(User.first_name).where(User.id == user_id))
    ).scalar_one_or_none()
    return html.escape(first_name or str(user_id))


@router.message(Command("victim"))
async def victim_command(message: Message, session: AsyncSession, bot: Bot) -> None:
    result = await victim_service.run_victim(session, message.chat.id)

    if result["winner"] is None:
        await message.answer("В чате пока нет активных участников для выбора жертвы дня.")
        return

    name = await _display_name(session, result["winner"])

    if not result["is_new"]:
        await message.answer(
            f"Сегодняшний {TITLE} уже выбран: <b>{name}</b> "
            "(приз уже выплачен, титул и дебафф действуют 24ч).",
            parse_mode="HTML",
        )
        return

    try:
        await tag_service.grant_title(
            bot,
            session,
            message.chat.id,
            result["winner"],
            TITLE,
            source="victim",
            expires_at=result["expires_at"],
        )
        await session.commit()
    except Exception:  # noqa: BLE001 - defensive, форма мута дуэли: приз уже выплачен
        # WR-05 (05-REVIEW.md): grant_title может упасть на DB-уровне (не
        # только на Telegram API — тот вообще не трогает сессию), оставляя
        # Postgres-транзакцию в aborted-состоянии. Раньше здесь не было
        # явного rollback — код полагался на то, что DbSessionMiddleware
        # закроет сессию и неявно подчистит после возврата хендлера, вместо
        # явной rollback-дисциплины, которой в проекте следуют для
        # DB-уровневых исключений.
        await session.rollback()
        logger.exception(
            "victim_command: не удалось выдать тег chat_id=%s user_id=%s",
            message.chat.id,
            result["winner"],
        )

    await message.answer(
        f"🎯 <b>{TITLE}: {name}</b>\n"
        f"Приз: {result['prize']} ювиков из банка чата.\n"
        f"Титул «{TITLE}» на 24ч.\n"
        "Дебафф: удвоенная комиссия перевода на 24ч.",
        parse_mode="HTML",
    )
