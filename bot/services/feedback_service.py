"""Фидбек участников чата (CASINO-03, D-04/D-05).

Тонкий сервис по форме `economy_service`/`markets_service`: `submit` делает
INSERT БЕЗ commit (транзакцию завершает вызывающий, форма `economy_service.
credit`), `list_feedback`/`set_resolved` — простые select/update, тот же
паттерн, что `list_feedback`/`set_resolved` markets_service не имеет прямого
аналога, но форма update+rowcount повторяет `economy_service._guarded_debit`.
"""

from __future__ import annotations

from sqlalchemy import insert
from sqlalchemy import select
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from common.models.feedback import Feedback

CATEGORIES: frozenset[str] = frozenset({"bug", "idea", "complaint", "other"})


class FeedbackError(Exception):
    """Базовое исключение модуля фидбека."""


class InvalidCategory(FeedbackError):
    """Категория не входит в CATEGORIES."""


async def submit(
    session: AsyncSession, chat_id: int, user_id: int, category: str, text: str
) -> None:
    """Сохраняет заявку фидбека. Автор (`chat_id`/`user_id`) должен браться
    вызывающим ИСКЛЮЧИТЕЛЬНО из `AuthContext` (IDOR, T-04.3-01) — эта функция
    сама не проверяет источник, дисциплина обеспечивается роутом. Не
    коммитит — транзакцию завершает вызывающий (форма `economy_service.
    credit`)."""
    if category not in CATEGORIES:
        raise InvalidCategory(f"Неизвестная категория: {category!r}")

    stmt = insert(Feedback).values(
        chat_id=chat_id, user_id=user_id, category=category, text=text
    )
    await session.execute(stmt)


async def list_feedback(session: AsyncSession, chat_id: int) -> list[dict]:
    """Все заявки фидбека чата, новые сверху."""
    stmt = (
        select(Feedback)
        .where(Feedback.chat_id == chat_id)
        .order_by(Feedback.created_at.desc())
    )
    result = await session.execute(stmt)
    return [
        {
            "id": row.id,
            "user_id": row.user_id,
            "category": row.category,
            "text": row.text,
            "resolved": row.resolved,
            "created_at": row.created_at,
        }
        for row in result.scalars().all()
    ]


async def set_resolved(
    session: AsyncSession, chat_id: int, feedback_id: int, resolved: bool
) -> bool:
    """Переключает статус resolved одной заявки. Возвращает False, если
    заявка с таким id (в этом chat_id) не найдена (rowcount == 0). Не
    коммитит — транзакцию завершает вызывающий."""
    stmt = (
        update(Feedback)
        .where(Feedback.chat_id == chat_id, Feedback.id == feedback_id)
        .values(resolved=resolved)
    )
    result = await session.execute(stmt)
    return result.rowcount == 1
