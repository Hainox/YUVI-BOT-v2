"""Сборка карточки участника `/card` (AI-03, D-04, WHATSNEW-подобный формат
2026-07-27) из блоков:

1. Числовая шапка — total_messages/active_days/first_active_date/
   last_active_date (ПРЯМОЕ переиспользование stats_service.get_user_stats,
   без дублирования SQL, D-04/Reusable Assets), средняя длина сообщения
   (get_user_avg_message_length, отдельный SQL — daily_stats не несёт длину
   текста, только message_count) и топ реакций получено/поставлено
   (reaction_service.get_top_reactions_received/given).
2. ОБРАЗ/ТЕМЫ/МАНЕРА/ЦИФРЫ — один LLM-вызов (build_profile_sections),
   числа из шапки передаются в промпт, чтобы ЦИФРЫ-блок интерпретировал
   реальные цифры, а не выдумывал свои.

build_portrait (короткий 2-4-предложенческий портрет) НЕ переиспользуется
здесь и остаётся НЕТРОНУТЫМ — его отдельно вызывает twin_service.build_twin_reply
(другой контракт: сырой текст для стиль-мимикрии двойника, не карточка).
"""

from __future__ import annotations

from sqlalchemy import func
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import settings
from bot.services import ai_client
from bot.services import reaction_service
from bot.services import settings_service
from bot.services import stats_service
from bot.services.summary_service import CHARS_PER_TOKEN
from bot.services.summary_service import build_context
from common.models.message import Message
from common.models.user import User

PORTRAIT_MESSAGE_LIMIT = 50
TOP_REACTIONS_LIMIT = 5
NO_DATA_PORTRAIT = "Пока маловато сообщений от этого участника для портрета — попробуйте позже."
NO_DATA_PROFILE = "Пока маловато сообщений от этого участника для карточки — попробуйте позже."


async def get_user_avg_message_length(session: AsyncSession, chat_id: int, user_id: int) -> float:
    """Средняя длина (символов) текстовых сообщений участника в чате —
    daily_stats несёт только message_count, не длину текста, поэтому это
    единственный блок карточки, читающий messages напрямую (тот же принцип,
    что get_user_nlp_averages делал раньше для sentiment/toxicity: прямой SQL
    по messages оправдан, когда daily_stats/word_frequency не несут нужного
    среза). 0.0, если у участника нет текстовых сообщений (без деления на ноль)."""
    stmt = select(func.avg(func.length(Message.text))).where(
        Message.chat_id == chat_id, Message.user_id == user_id, Message.text.is_not(None)
    )
    result = await session.execute(stmt)
    avg_length = result.scalar_one()
    return float(avg_length) if avg_length is not None else 0.0


async def fetch_user_recent_texts(
    session: AsyncSession, chat_id: int, user_id: int, n: int
) -> list[dict]:
    """Последние N текстовых сообщений КОНКРЕТНОГО участника, в хронологическом
    порядке (от старых к новым — build_context ожидает именно такой порядок).

    Аналог summary_service.fetch_recent_texts + фильтр по user_id (per-card
    вариант; здесь намеренно НЕ переиспользуется fetch_recent_texts напрямую,
    т.к. у неё нет параметра user_id — добавлять его туда расширило бы
    контракт функции, используемой /summary для всего чата).

    Публичная (WR-04, 05-REVIEW.md) — вызывается не только build_portrait
    отсюда, но и напрямую из twin_service.build_twin_reply; leading-
    underscore имя неверно сигнализировало бы "internal only", хотя у
    функции есть реальный внешний вызывающий.
    """
    stmt = (
        select(Message.text, User.first_name)
        .outerjoin(User, User.id == Message.user_id)
        .where(Message.chat_id == chat_id, Message.user_id == user_id, Message.text.is_not(None))
        .order_by(Message.created_at.desc(), Message.id.desc())
        .limit(n)
    )
    result = await session.execute(stmt)
    rows = list(reversed(result.all()))
    return [{"author": row.first_name or "Аноним", "text": row.text} for row in rows]


async def build_portrait(
    session: AsyncSession, chat_id: int, user_id: int, display_name: str
) -> str:
    """Короткий юмористический портрет участника по истории его сообщений
    (AI-03/D-04, блок 1). При отсутствии сообщений — заглушка, не падение.

    Стриминг в сообщение не нужен (портрет короткий, часть большого ответа
    /card) — собираем полный текст из ai_client.stream и возвращаем строкой.
    """
    rows = await fetch_user_recent_texts(session, chat_id, user_id, PORTRAIT_MESSAGE_LIMIT)
    if not rows:
        return NO_DATA_PORTRAIT

    char_budget = settings.ai_max_input_tokens * CHARS_PER_TOKEN
    context = build_context(rows, char_budget)

    system_prompt = await settings_service.get_active_prompt(session, chat_id)
    system_prompt += (
        f"\n\nНа основе сообщений участника {display_name} напиши короткий "
        "юмористический портрет — какой этот человек в чате, 2-4 предложения, "
        "по-доброму, без оскорблений."
    )
    model = await settings_service.get_active_model(session, chat_id, default=settings.ai_structured_model)
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": context},
    ]

    return await ai_client.complete_with_fallback(
        messages, primary_model=model, max_tokens=settings.ai_max_output_tokens
    )


_PROFILE_SECTIONS_PROMPT_TEMPLATE = (
    "\n\nНа основе сообщений участника {display_name} и статистики ниже "
    "напиши карточку участника чата в энергичном, ироничном стиле "
    "разбора а-ля «+100500» — живой язык, мемы, сарказм; мат уместен "
    "(участники чата 18+), но это разбор человека, а не оскорбление, без "
    "реальной травли.\n\n"
    "Статистика участника: всего сообщений {total_messages}, активных дней "
    "{active_days}, период активности с {first_active_date} по "
    "{last_active_date}, средняя длина сообщения {avg_length:.0f} символов, "
    "топ полученных реакций: {received}, топ поставленных реакций: {given}.\n\n"
    "Ответь СТРОГО в этом формате — четыре секции с ЭТИМИ заголовками "
    "заглавными буквами каждый на своей строке, без разметки markdown/HTML:\n"
    "ОБРАЗ\n(2-4 предложения — кто этот человек в чате)\n\n"
    "ТЕМЫ\n(3-5 пунктов списком через дефис — о чём этот человек обычно "
    "пишет, по факту его же сообщений)\n\n"
    "МАНЕРА\n(2-3 предложения — стиль письма: длина фраз, характерные "
    "обороты, сленг, эмоции)\n\n"
    "ЦИФРЫ\n(2-3 предложения — саркастичная интерпретация статистики "
    "участника выше, не пересказывай числа построчно — обыграй их)"
)


def _format_reaction_counts(reactions: list[dict]) -> str:
    if not reactions:
        return "нет данных"
    return ", ".join(f"{row['emoji']}×{row['count']}" for row in reactions)


async def build_profile_sections(
    session: AsyncSession,
    chat_id: int,
    user_id: int,
    display_name: str,
    stats: dict,
    avg_length: float,
    reactions_received: list[dict],
    reactions_given: list[dict],
) -> str:
    """ОБРАЗ/ТЕМЫ/МАНЕРА/ЦИФРЫ одним LLM-вызовом — реальные числа шапки
    карточки передаются в промпт, чтобы блок ЦИФРЫ интерпретировал их, а не
    выдумывал свои (та же дисциплина, что не даёт LLM галлюцинировать
    финансовые/игровые данные в других AI-хендлерах). При отсутствии
    сообщений — заглушка, не падение (см. NO_DATA_PROFILE)."""
    rows = await fetch_user_recent_texts(session, chat_id, user_id, PORTRAIT_MESSAGE_LIMIT)
    if not rows:
        return NO_DATA_PROFILE

    char_budget = settings.ai_max_input_tokens * CHARS_PER_TOKEN
    context = build_context(rows, char_budget)

    system_prompt = await settings_service.get_active_prompt(session, chat_id)
    system_prompt += _PROFILE_SECTIONS_PROMPT_TEMPLATE.format(
        display_name=display_name,
        total_messages=stats["total_messages"],
        active_days=stats["active_days"],
        first_active_date=stats["first_active_date"],
        last_active_date=stats["last_active_date"],
        avg_length=avg_length,
        received=_format_reaction_counts(reactions_received),
        given=_format_reaction_counts(reactions_given),
    )
    model = await settings_service.get_active_model(session, chat_id, default=settings.ai_structured_model)
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": context},
    ]

    return (
        await ai_client.complete_with_fallback(
            messages, primary_model=model, max_tokens=settings.ai_max_output_tokens
        )
    ).strip()


async def build_card(session: AsyncSession, chat_id: int, user_id: int, display_name: str) -> dict:
    """Собирает карточку участника (D-04, WHATSNEW-подобный формат
    2026-07-27): числовая шапка (stats/avg_length/reactions) + ОБРАЗ/ТЕМЫ/
    МАНЕРА/ЦИФРЫ одним LLM-вызовом.

    stats-блок — ПРЯМОЙ вызов stats_service.get_user_stats (без дублирующего
    SQL по daily_stats здесь)."""
    user_stats = await stats_service.get_user_stats(session, chat_id, user_id)
    avg_length = await get_user_avg_message_length(session, chat_id, user_id)
    reactions_received = await reaction_service.get_top_reactions_received(
        session, chat_id, user_id, limit=TOP_REACTIONS_LIMIT
    )
    reactions_given = await reaction_service.get_top_reactions_given(
        session, chat_id, user_id, limit=TOP_REACTIONS_LIMIT
    )

    profile = await build_profile_sections(
        session,
        chat_id,
        user_id,
        display_name,
        user_stats,
        avg_length,
        reactions_received,
        reactions_given,
    )

    return {
        "stats": user_stats,
        "avg_length": avg_length,
        "reactions_received": reactions_received,
        "reactions_given": reactions_given,
        "profile": profile,
    }
