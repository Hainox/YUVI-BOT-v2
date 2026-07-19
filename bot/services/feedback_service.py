"""Фидбек участников чата (CASINO-03, D-04/D-05; close()/reward — FEEDBACK-01, D-14).

Тонкий сервис по форме `economy_service`/`markets_service`: `submit` делает
INSERT БЕЗ commit (транзакцию завершает вызывающий, форма `economy_service.
credit`), `list_feedback`/`set_resolved` — простые select/update, тот же
паттерн, что `list_feedback`/`set_resolved` markets_service не имеет прямого
аналога, но форма update+rowcount повторяет `economy_service._guarded_debit`.

`close()` расширяет `set_resolved` наградой автору (D-14): bug/idea начисляют
ювики напрямую в баланс через `economy_service.credit` (mint, не из банка
чата), complaint/other закрываются без награды. НЕ коммитит — форма всех
остальных функций модуля.
"""

from __future__ import annotations

from sqlalchemy import func
from sqlalchemy import insert
from sqlalchemy import select
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import settings
from bot.services import economy_service
from common.models.feedback import Feedback

CATEGORIES: frozenset[str] = frozenset({"bug", "idea", "complaint", "other"})

# Награда автору при close() по категории (D-14): bug/idea только —
# complaint/other намеренно отсутствуют в словаре, `.get(category, 0)` в
# close() ниже трактует их как «без награды».
FEEDBACK_REWARD_BY_CATEGORY: dict[str, int] = {
    "bug": settings.feedback_reward_bug,
    "idea": settings.feedback_reward_idea,
}


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


async def close(session: AsyncSession, chat_id: int, feedback_id: int) -> bool:
    """Закрывает заявку (resolved=True) и при первом закрытии начисляет
    награду автору напрямую в баланс (не из банка) через `economy_service.
    credit` — bug/idea по `FEEDBACK_REWARD_BY_CATEGORY`, complaint/other — 0
    (D-14).

    Идемпотентно: `rewarded_at IS NOT NULL` — явный якорь повторного вызова
    (T-06-13), повторный `close()` той же заявки — no-op, деньги не
    двигаются повторно. Дублирует защиту `economy_service.credit(ref_id=
    f"feedback_reward:{feedback_id}")`, но избегает лишнего похода в
    `economy_tx` на повторных вызовах. Возвращает False, только если заявка
    с таким id (в этом chat_id) не найдена (rowcount 0 — форма
    `set_resolved`). Не коммитит — транзакцию завершает вызывающий."""
    row = (
        await session.execute(
            select(Feedback)
            .where(Feedback.chat_id == chat_id, Feedback.id == feedback_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if row is None:
        return False
    if row.rewarded_at is not None:
        return True

    amount = FEEDBACK_REWARD_BY_CATEGORY.get(row.category, 0)
    if amount > 0:
        await economy_service.credit(
            session,
            chat_id,
            row.user_id,
            amount,
            kind="feedback_reward",
            ref_id=f"feedback_reward:{feedback_id}",
        )

    await session.execute(
        update(Feedback)
        .where(Feedback.chat_id == chat_id, Feedback.id == feedback_id)
        .values(resolved=True, reward=amount, rewarded_at=func.now())
    )
    return True
