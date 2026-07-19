"""AI-двойник `/twin` (TWIN-01/02, D-01/D-02). Тонкий хендлер: резолвит цель
(reply > text_mention > @username/id-аргумент > сам вызывающий — форма
ai_card.py::_resolve_target), зовёт twin_service.build_twin_reply и хардкодит
дисклеймер '🤖 Двойник {Имя}:' ЗДЕСЬ (Pitfall 8) — сервис никогда не отдаёт
префикс на генерацию модели.

Пять команд согласия (/twin_optin /twin_optout /twin_pause /twin_resume
/twin_status) действуют ТОЛЬКО на message.from_user.id — любой @arg,
переданный этим командам, игнорируется (V4, T-05-04); только `/twin @user`
берёт цель, и лишь для чтения consent.
"""

from __future__ import annotations

import html

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.services import twin_service
from common.models.user import User

router = Router()


def _parse_target_arg(message: Message) -> str | None:
    """Необязательный аргумент `/twin @username` или `/twin 12345`."""
    if message.text is None:
        return None
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        return None
    arg = parts[1].strip()
    return arg or None


async def _resolve_by_arg(session: AsyncSession, arg: str) -> tuple[int, str] | None:
    """Резолвит `@username` или числовой id через таблицу users."""
    if arg.startswith("@"):
        stmt = select(User.id, User.first_name).where(User.username == arg[1:])
    elif arg.lstrip("-").isdigit():
        stmt = select(User.id, User.first_name).where(User.id == int(arg))
    else:
        return None

    row = (await session.execute(stmt)).first()
    if row is None:
        return None
    return row.id, row.first_name or str(row.id)


async def _resolve_target(message: Message, session: AsyncSession) -> tuple[int, str] | None:
    """Резолв цели `/twin`: reply > text_mention > @username/id-аргумент > сам
    вызывающий (в этом приоритете). Возвращает (user_id, НЕ экранированное
    display_name) либо None, если аргумент указан, но участник не найден."""
    if message.reply_to_message is not None and message.reply_to_message.from_user is not None:
        user = message.reply_to_message.from_user
        return user.id, user.first_name or str(user.id)

    if message.entities:
        for entity in message.entities:
            if entity.type == "text_mention" and entity.user is not None:
                user = entity.user
                return user.id, user.first_name or str(user.id)

    arg = _parse_target_arg(message)
    if arg is not None:
        return await _resolve_by_arg(session, arg)

    if message.from_user is None:
        return None
    user = message.from_user
    return user.id, user.first_name or str(user.id)


@router.message(Command("twin"))
async def twin_command(message: Message, session: AsyncSession) -> None:
    target = await _resolve_target(message, session)
    if target is None:
        await message.answer("Участник не найден.")
        return

    user_id, raw_name = target
    try:
        raw_text = await twin_service.build_twin_reply(
            session, message.chat.id, user_id, raw_name
        )
    except twin_service.TwinConsentError:
        await message.answer("Этот участник не подключил Двойника.")
        return

    # D-02/Pitfall 8: дисклеймер хардкожен ЗДЕСЬ, независимо от текста модели.
    text = f"🤖 Двойник {html.escape(raw_name)}: {html.escape(raw_text)}"
    await message.answer(text, parse_mode="HTML")  # всегда отвечаем прямо в группе


@router.message(Command("twin_optin"))
async def twin_optin(message: Message, session: AsyncSession) -> None:
    """Подключает Двойника ТОЛЬКО для вызывающего (V4) — любой @arg игнорируется."""
    if message.from_user is None:
        return
    await twin_service.set_opt_in(session, message.chat.id, message.from_user.id, "active")
    await session.commit()
    await message.answer(
        "Двойник подключён. /twin_pause — приостановить, /twin_optout — отключить совсем."
    )


@router.message(Command("twin_pause"))
async def twin_pause(message: Message, session: AsyncSession) -> None:
    if message.from_user is None:
        return
    await twin_service.set_opt_in(session, message.chat.id, message.from_user.id, "paused")
    await session.commit()
    await message.answer("Двойник на паузе. /twin_resume — вернуть его обратно.")


@router.message(Command("twin_resume"))
async def twin_resume(message: Message, session: AsyncSession) -> None:
    if message.from_user is None:
        return
    await twin_service.set_opt_in(session, message.chat.id, message.from_user.id, "active")
    await session.commit()
    await message.answer("Двойник снова активен.")


@router.message(Command("twin_optout"))
async def twin_optout(message: Message, session: AsyncSession) -> None:
    if message.from_user is None:
        return
    await twin_service.opt_out(session, message.chat.id, message.from_user.id)
    await session.commit()
    await message.answer("Двойник отключён. /twin_optin — подключить заново в любой момент.")


@router.message(Command("twin_status"))
async def twin_status(message: Message, session: AsyncSession) -> None:
    if message.from_user is None:
        return
    status = await twin_service.get_status(session, message.chat.id, message.from_user.id)
    if status == "active":
        text = "Двойник активен."
    elif status == "paused":
        text = "Двойник на паузе. /twin_resume — вернуть."
    else:
        text = "Двойник не подключён. /twin_optin — подключить."
    await message.answer(text)
