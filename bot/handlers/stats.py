"""Статистические команды (STATS-01). Тонкий хендлер: парсит вход, зовёт
stats_service, форматирует и отвечает — вся логика в сервисе.

Все пользовательские имена (first_name/username) прогоняются через
html.escape перед вставкой в HTML-ответ (ASVS V5 — first_name может
легально содержать `<`, `>`, `&`).
"""

from __future__ import annotations

import html

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.services import stats_service

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


def _display_name(message: Message) -> str:
    """Отображаемое имя вызывающего, безопасное для HTML-вывода."""
    user = message.from_user
    assert user is not None
    return html.escape(user.first_name or str(user.id))


def _period_label(days: int | None) -> str:
    return f"за последние {days} дн." if days is not None else "за всё время"


def format_user_stats(display_name: str, stats: dict, days: int | None) -> str:
    period = _period_label(days)
    lines = [
        f"<b>Статистика {display_name}</b> ({period})",
        f"Сообщений: {stats['total_messages']}",
        f"Активных дней: {stats['active_days']}",
    ]
    if stats["first_active_date"] is not None and stats["last_active_date"] is not None:
        lines.append(f"Первая активность: {stats['first_active_date'].strftime('%d.%m.%Y')}")
        lines.append(f"Последняя активность: {stats['last_active_date'].strftime('%d.%m.%Y')}")
    return "\n".join(lines)


def format_top_participants(rows: list[dict], days: int | None) -> str:
    period = _period_label(days)
    if not rows:
        return f"Нет данных по участникам ({period})."
    lines = [f"<b>Топ участников</b> ({period})"]
    for i, row in enumerate(rows, start=1):
        name = html.escape(row["first_name"] or str(row["user_id"]))
        lines.append(f"{i}. {name} — {row['message_count']}")
    return "\n".join(lines)


def format_top_words(rows: list[dict]) -> str:
    if not rows:
        return "<b>Топ слов</b>\nНет данных."
    lines = ["<b>Топ слов</b>"]
    for i, row in enumerate(rows, start=1):
        lines.append(f"{i}. {html.escape(row['word'])} — {row['count']}")
    return "\n".join(lines)


def format_chat_stats(
    total: int,
    top_participants: list[dict],
    top_words: list[dict],
    days: int | None,
) -> str:
    period = _period_label(days)
    lines = [
        f"<b>Статистика чата</b> ({period})",
        f"Всего сообщений: {total}",
        "",
        format_top_participants(top_participants, days),
        "",
        format_top_words(top_words),
    ]
    return "\n".join(lines)


def format_streak(display_name: str, streak: int) -> str:
    if streak == 0:
        return f"{display_name}, пока нет активной серии дней подряд."
    return f"{display_name}, текущая серия: {streak} дн. подряд."


def format_peak_day(peak: tuple | None, days: int | None) -> str:
    period = _period_label(days)
    if peak is None:
        return f"Нет данных за период ({period})."
    peak_date, count = peak
    return f"Самый активный день ({period}): {peak_date.strftime('%d.%m.%Y')} — {count} сообщ."


@router.message(Command("mystats"))
async def mystats_command(message: Message, session: AsyncSession) -> None:
    if message.from_user is None:
        return

    days = _parse_days_arg(message)
    stats = await stats_service.get_user_stats(session, message.chat.id, message.from_user.id, days)
    text = format_user_stats(_display_name(message), stats, days)

    await message.answer(text, parse_mode="HTML")  # D-05: всегда отвечаем прямо в группе


@router.message(Command("chatstats"))
async def chatstats_command(message: Message, session: AsyncSession) -> None:
    if message.from_user is None:
        return

    days = _parse_days_arg(message)
    total = await stats_service.get_chat_message_count(session, message.chat.id, days)
    top_participants = await stats_service.get_top_participants(session, message.chat.id, days, limit=5)
    top_words = await stats_service.get_top_words(session, message.chat.id, days, limit=5)
    text = format_chat_stats(total, top_participants, top_words, days)

    await message.answer(text, parse_mode="HTML")  # D-05: всегда отвечаем прямо в группе


@router.message(Command("who"))
async def who_command(message: Message, session: AsyncSession) -> None:
    if message.from_user is None:
        return

    days = _parse_days_arg(message)
    top_participants = await stats_service.get_top_participants(session, message.chat.id, days, limit=10)
    text = format_top_participants(top_participants, days)

    await message.answer(text, parse_mode="HTML")  # D-05: всегда отвечаем прямо в группе


@router.message(Command("streak"))
async def streak_command(message: Message, session: AsyncSession) -> None:
    if message.from_user is None:
        return

    streak = await stats_service.get_streak(session, message.chat.id, message.from_user.id)
    text = format_streak(_display_name(message), streak)

    await message.answer(text, parse_mode="HTML")  # D-05: всегда отвечаем прямо в группе


@router.message(Command("peakday"))
async def peakday_command(message: Message, session: AsyncSession) -> None:
    if message.from_user is None:
        return

    days = _parse_days_arg(message)
    peak = await stats_service.get_peak_day(session, message.chat.id, days)
    text = format_peak_day(peak, days)

    await message.answer(text, parse_mode="HTML")  # D-05: всегда отвечаем прямо в группе
