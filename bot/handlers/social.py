"""Команды соцмагазина (SHOP-01): `/poke` `/hug` `/joke_order` `/roast` —
платные взаимодействия участника с другим участником чата. Тонкий хендлер:
резолвит цель через `target_resolution_service.resolve_target`, отвергает
самонацеливание (D-03) ДО обращения к сервису и ДО списания денег, зовёт
соответствующий `social_service.do_*`, коммитит и отвечает в чат с
`html.escape` имён/текста — форма `bot/handlers/duel.py` (resolve_target +
self-guard + `parse_mode="HTML"`) и AI-хендлеров `/topics`/`/phrase`/`/joke`
(`html.escape` LLM-вывода, T-02-15).
"""

from __future__ import annotations

import html
import logging

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.services import economy_service
from bot.services import social_service
from bot.services.target_resolution_service import resolve_target

logger = logging.getLogger(__name__)

router = Router()


def _extract_arg(message: Message) -> str | None:
    """Текст после команды (реплай/@username/тема)."""
    if message.text is None:
        return None
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        return None
    return parts[1].strip() or None


async def _resolve_target_or_reply(
    message: Message, session: AsyncSession, target_arg: str | None, usage: str
) -> tuple[int, str] | None:
    target = await resolve_target(message, session, target_arg)
    if target is None:
        await message.answer(usage)
        return None
    return target


async def _guard_self_target(message: Message, target_id: int, verb: str) -> bool:
    """D-03: самонацеливание отвергается ДО любого списания. Возвращает
    True, если цель прошла проверку (можно продолжать)."""
    assert message.from_user is not None
    if target_id == message.from_user.id:
        await message.answer(f"Нельзя {verb} самого себя 🙃")
        return False
    return True


@router.message(Command("poke"))
async def poke_command(message: Message, session: AsyncSession) -> None:
    if message.from_user is None:
        return

    target = await _resolve_target_or_reply(
        message,
        session,
        _extract_arg(message),
        "Ответьте на сообщение цели командой /poke или используйте /poke @username.",
    )
    if target is None:
        return
    target_id, target_name = target
    if not await _guard_self_target(message, target_id, "тыкать"):
        return

    try:
        text = await social_service.do_poke(
            session,
            message.chat.id,
            message.from_user.id,
            target_id,
            html.escape(target_name),
            str(message.message_id),
        )
    except economy_service.InsufficientFunds:
        await message.answer("Недостаточно ювиков.")
        return

    await session.commit()
    if text is None:
        # Повтор апдейта Telegram (тот же message_id) — списание уже
        # применено ранее, повторное сообщение в чат не отправляем (WR-02
        # 06-REVIEW.md).
        return
    await message.answer(text, parse_mode="HTML")


@router.message(Command("hug"))
async def hug_command(message: Message, session: AsyncSession) -> None:
    if message.from_user is None:
        return

    target = await _resolve_target_or_reply(
        message,
        session,
        _extract_arg(message),
        "Ответьте на сообщение цели командой /hug или используйте /hug @username.",
    )
    if target is None:
        return
    target_id, target_name = target
    if not await _guard_self_target(message, target_id, "обнимать"):
        return

    try:
        text = await social_service.do_hug(
            session,
            message.chat.id,
            message.from_user.id,
            target_id,
            html.escape(target_name),
            str(message.message_id),
        )
    except economy_service.InsufficientFunds:
        await message.answer("Недостаточно ювиков.")
        return

    await session.commit()
    if text is None:
        return
    await message.answer(text, parse_mode="HTML")


@router.message(Command("joke_order"))
async def joke_order_command(message: Message, session: AsyncSession) -> None:
    if message.from_user is None:
        return

    raw_arg = _extract_arg(message)
    if raw_arg is None:
        await message.answer(
            "Использование: /joke_order <тема> (ответом на сообщение цели) "
            "или /joke_order @username <тема>."
        )
        return

    # Ответ на сообщение цели -> весь аргумент это тема; иначе первый токен
    # (@username/id) уходит на резолв цели, остаток — тема (D-04).
    if message.reply_to_message is not None:
        target_arg, topic = None, raw_arg
    else:
        tokens = raw_arg.split(maxsplit=1)
        target_arg = tokens[0]
        topic = tokens[1].strip() if len(tokens) > 1 else ""

    if not topic:
        await message.answer("Укажите тему анекдота, например: /joke_order @username про котиков")
        return

    target = await _resolve_target_or_reply(
        message,
        session,
        target_arg,
        "Ответьте на сообщение цели командой /joke_order <тема> или используйте /joke_order @username <тема>.",
    )
    if target is None:
        return
    target_id, target_name = target
    if not await _guard_self_target(message, target_id, "заказывать анекдот для"):
        return

    try:
        text = await social_service.do_joke_order(
            session,
            message.chat.id,
            message.from_user.id,
            target_id,
            html.escape(target_name),
            topic,
            str(message.message_id),
        )
    except economy_service.InsufficientFunds:
        await message.answer("Недостаточно ювиков.")
        return

    await session.commit()
    if text is None:
        return
    await message.answer(html.escape(text), parse_mode="HTML")


@router.message(Command("roast"))
async def roast_command(message: Message, session: AsyncSession) -> None:
    if message.from_user is None:
        return

    target = await _resolve_target_or_reply(
        message,
        session,
        _extract_arg(message),
        "Ответьте на сообщение цели командой /roast или используйте /roast @username.",
    )
    if target is None:
        return
    target_id, target_name = target
    if not await _guard_self_target(message, target_id, "роастить"):
        return

    try:
        text = await social_service.do_roast(
            session,
            message.chat.id,
            message.from_user.id,
            target_id,
            html.escape(target_name),
            str(message.message_id),
        )
    except economy_service.InsufficientFunds:
        await message.answer("Недостаточно ювиков.")
        return

    await session.commit()
    if text is None:
        return
    await message.answer(html.escape(text), parse_mode="HTML")
