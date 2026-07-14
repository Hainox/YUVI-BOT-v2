"""Карточка участника `/card` (AI-03, D-04). Тонкий хендлер: резолвит цель
(reply / @username или id / сам вызывающий), зовёт card_service.build_card,
форматирует три блока и отвечает — вся сборка (AI-портрет + stats_service +
NLP-средние) живёт в сервисе.

AI-портрет — текст LLM, поэтому ОБЯЗАТЕЛЬНО проходит через html.escape перед
parse_mode="HTML" (T-02-19, конвенция stats.py) — как и любое имя участника.
"""

from __future__ import annotations

import html

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.services import card_service
from common.models.user import User

router = Router()


def _parse_target_arg(message: Message) -> str | None:
    """Необязательный аргумент `/card @username` или `/card 12345`."""
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
    """Резолв цели `/card`: reply > text_mention > @username/id-аргумент > сам
    вызывающий (в этом приоритете). Возвращает (user_id, НЕ экранированное
    display_name) либо None, если аргумент указан, но участник не найден.
    """
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


def format_card(display_name: str, card: dict) -> str:
    """Рендерит три блока карточки (D-04). Портрет — LLM-текст, экранируется
    здесь (не в card_service — сервис не должен знать про HTML-рендеринг)."""
    stats = card["stats"]
    nlp = card["nlp"]

    lines = [
        f"<b>Карточка участника: {display_name}</b>",
        "",
        "🎭 <b>Портрет</b>",
        html.escape(card["portrait"]),
        "",
        "📊 <b>Статистика</b>",
        f"Сообщений: {stats['total_messages']}",
        f"Активных дней: {stats['active_days']}",
        f"Текущая серия: {stats['streak']} дн. подряд",
    ]
    if stats["top_words"]:
        words = ", ".join(html.escape(row["word"]) for row in stats["top_words"])
        lines.append(f"Топ слов чата: {words}")

    lines.append("")
    lines.append("💬 <b>Настроение/токсичность</b>")
    if nlp["classified_count"] == 0:
        lines.append("Пока недостаточно данных.")
    else:
        if nlp["avg_sentiment"] is not None:
            lines.append(f"Среднее настроение: {nlp['avg_sentiment']:.2f}")
        if nlp["avg_toxicity"] is not None:
            lines.append(f"Средняя токсичность: {nlp['avg_toxicity']:.2f}")
        lines.append(f"Проанализировано сообщений: {nlp['classified_count']}")

    return "\n".join(lines)


@router.message(Command("card"))
async def card_command(message: Message, session: AsyncSession) -> None:
    target = await _resolve_target(message, session)
    if target is None:
        await message.answer("Участник не найден.")
        return

    user_id, raw_name = target
    card = await card_service.build_card(session, message.chat.id, user_id, raw_name)
    display_name = html.escape(raw_name)
    text = format_card(display_name, card)

    await message.answer(text, parse_mode="HTML")  # D-05: всегда отвечаем прямо в группе
