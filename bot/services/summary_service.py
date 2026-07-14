"""Сборка промпта и обрезка контекста для /summary, /sum, /summary_custom, /sumc (AI-01/AI-07).

Читает последние N сообщений чата, склеивает их в "Имя: текст" построчно,
обрезает по символьному бюджету С НАЧАЛА (самые старые строки уходят первыми) —
самые свежие сообщения всегда остаются в контексте (AI-07). Точного токенайзера
для моделей каталога Go нет (DeepSeek/GLM/Qwen используют разные токенайзеры),
поэтому бюджет — char/4 эвристика от ai_max_input_tokens (RESEARCH.md
Anti-Patterns), не exact token counting.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import settings
from bot.services import ai_client
from bot.services import settings_service
from common.models.message import Message
from common.models.user import User

CHARS_PER_TOKEN = 4


async def fetch_recent_texts(session: AsyncSession, chat_id: int, n: int) -> list[dict]:
    """Последние N текстовых сообщений чата с именем автора, в хронологическом
    порядке (от старых к новым — build_context ожидает именно такой порядок)."""
    stmt = (
        select(Message.text, User.first_name)
        .outerjoin(User, User.id == Message.user_id)
        .where(Message.chat_id == chat_id, Message.text.is_not(None))
        .order_by(Message.created_at.desc(), Message.id.desc())
        .limit(n)
    )
    result = await session.execute(stmt)
    rows = list(reversed(result.all()))
    return [{"author": row.first_name or "Аноним", "text": row.text} for row in rows]


def build_context(rows: list[dict], char_budget: int) -> str:
    """Склеивает строки вида "Имя: текст" построчно, обрезая с начала (самые
    старые) пока итог не влезет в char_budget. rows — хронологический порядок
    (от старых к новым), сохраняются самые свежие (AI-07)."""
    lines = [f"{row['author']}: {row['text']}" for row in rows]
    total = sum(len(line) + 1 for line in lines)  # +1 за перевод строки

    while lines and total > char_budget:
        removed = lines.pop(0)
        total -= len(removed) + 1

    return "\n".join(lines)


async def stream_summary(
    session: AsyncSession,
    chat_id: int,
    n: int,
    focus: str | None,
) -> AsyncIterator[str]:
    """Стримит краткий пересказ последних N сообщений чата (опционально — с
    фокусом на конкретную тему). Модель и системный промпт читаются из
    bot_settings (AI-08) — переключение модели админом сразу влияет на /summary."""
    rows = await fetch_recent_texts(session, chat_id, n)
    char_budget = settings.ai_max_input_tokens * CHARS_PER_TOKEN
    context = build_context(rows, char_budget)

    system_prompt = await settings_service.get_active_prompt(session, chat_id)
    system_prompt += "\n\nСделай краткий пересказ переписки на русском языке."
    if focus:
        clipped_focus = focus[: settings.ai_max_custom_prompt_chars]
        system_prompt += f" Сфокусируйся на: {clipped_focus}"

    model = await settings_service.get_active_model(session, chat_id)
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": context or "Сообщений нет."},
    ]

    async for delta in ai_client.stream(messages, model=model, max_tokens=settings.ai_max_output_tokens):
        yield delta
