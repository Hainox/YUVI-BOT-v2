"""/fb <текст> — участник отправляет заявку фидбека прямо из чата (FEEDBACK-01,
D-13). Тонкий хендлер: парсит опциональный префикс категории (Claude's
discretion), зовёт `feedback_service.submit` — автор строго из
`message.from_user`, никогда из текста заявки. Router подхватывается
`bot/main.py::_discover_routers` автоматически.
"""

from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command
from aiogram.filters import CommandObject
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.services import feedback_service

router = Router()

_USAGE_HINT = "Использование: /fb <текст> (можно начать с категории: bug/idea/complaint)"


def _parse_fb_args(args: str) -> tuple[str, str]:
    """Опциональный префикс категории (D-13, Claude's discretion): первое
    слово ∈ `feedback_service.CATEGORIES` → (эта_категория, остаток текста),
    иначе вся заявка попадает в категорию `"other"`."""
    tokens = args.strip().split(maxsplit=1)
    if tokens and tokens[0] in feedback_service.CATEGORIES:
        category = tokens[0]
        text = tokens[1] if len(tokens) > 1 else ""
        return category, text
    return "other", args.strip()


@router.message(Command("fb"))
async def cmd_fb(message: Message, session: AsyncSession, command: CommandObject) -> None:
    if message.from_user is None:
        return
    if not command.args or not command.args.strip():
        await message.reply(_USAGE_HINT)
        return

    category, text = _parse_fb_args(command.args)
    if not text.strip():
        await message.reply(_USAGE_HINT)
        return

    await feedback_service.submit(session, message.chat.id, message.from_user.id, category, text)
    await session.commit()
    await message.reply("Спасибо, заявка принята!")
