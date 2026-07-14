"""Настроение/токсичность чата (NLP-03). Тонкий хендлер: парсит вход, зовёт
mood_service (ЧИСТЫЙ SQL над заранее посчитанными колонками), форматирует
и отвечает — вся логика в сервисе.

RESEARCH.md Anti-Patterns: /mood и /toxic НИКОГДА не зовут LLM/NLP синхронно
(этим занимается фоновый job плана 02-05) — этот модуль не импортирует
клиенты внешних AI/NLP-сервисов.
"""

from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.services import mood_service

router = Router()


def _parse_days_arg(message: Message) -> int | None:
    """Парсит необязательный числовой аргумент `/команда N` (D-06)."""
    if message.text is None:
        return None
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        return None
    arg = parts[1].strip()
    if not arg.isdigit():
        return None
    return int(arg)


def _period_label(days: int | None) -> str:
    return f"за последние {days} дн." if days is not None else "за всё время"


def format_mood(mood: dict, days: int | None) -> str:
    period = _period_label(days)
    if mood["classified_count"] == 0:
        return f"Пока недостаточно данных о настроении чата ({period})."

    shares = mood["label_shares"]
    lines = [
        f"<b>Настроение чата</b> ({period})",
        f"Позитивных: {shares['positive']:.0%}",
        f"Нейтральных: {shares['neutral']:.0%}",
        f"Негативных: {shares['negative']:.0%}",
        f"Средняя уверенность классификации: {mood['avg_sentiment']:.2f}",
        f"Проанализировано сообщений: {mood['classified_count']}",
    ]
    return "\n".join(lines)


def format_toxic(toxicity: dict, days: int | None) -> str:
    period = _period_label(days)
    if toxicity["classified_count"] == 0:
        return f"Пока недостаточно данных о токсичности чата ({period})."

    lines = [
        f"<b>Токсичность чата</b> ({period})",
        f"Средний показатель токсичности: {toxicity['avg_toxicity']:.2f}",
        f"Доля токсичных сообщений: {toxicity['toxic_share']:.0%}",
        f"Проанализировано сообщений: {toxicity['classified_count']}",
    ]
    return "\n".join(lines)


@router.message(Command("mood"))
async def mood_command(message: Message, session: AsyncSession) -> None:
    if message.from_user is None:
        return

    days = _parse_days_arg(message)
    mood = await mood_service.get_chat_mood(session, message.chat.id, days)
    text = format_mood(mood, days)

    await message.answer(text, parse_mode="HTML")  # D-05: всегда отвечаем прямо в группе


@router.message(Command("toxic"))
async def toxic_command(message: Message, session: AsyncSession) -> None:
    if message.from_user is None:
        return

    days = _parse_days_arg(message)
    toxicity = await mood_service.get_chat_toxicity(session, message.chat.id, days)
    text = format_toxic(toxicity, days)

    await message.answer(text, parse_mode="HTML")  # D-05: всегда отвечаем прямо в группе
