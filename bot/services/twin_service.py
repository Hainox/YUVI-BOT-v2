"""AI-двойник `/twin` (TWIN-01/02) — реализация по AI-SPEC §3/§4: one-shot
стиль-мимикрия без нового фреймворка (reuse ai_client/card_service/
summary_service/settings_service).

Consent-гейт (`_check_consent`) — ПЕРВАЯ строка `build_twin_reply`, structurally
до card_service.build_portrait и до любого LLM-вызова (Pitfall 5, AI-SPEC §1
Critical Failure Mode #1) — не-opted-in/paused участник не должен быть прочитан.

Дисклеймер-префикс НЕ формируется здесь — build_twin_reply возвращает СЫРОЙ
текст модели, хендлер (bot/handlers/twin.py) хардкодит префикс (D-02,
Pitfall 8): модели не доверяем самораскрытие.

Профиль = card_service.build_portrait (14-дн психо-портрет, reuse as-is,
D-03) + свежая budget-capped выборка реальных сообщений через
card_service.fetch_user_recent_texts + summary_service.build_context (тот
же char-budget дисциплина, что и /summary).
"""

from __future__ import annotations

from sqlalchemy import delete
from sqlalchemy import func
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import settings
from bot.services import ai_client
from bot.services import card_service
from bot.services import settings_service
from bot.services.summary_service import CHARS_PER_TOKEN
from bot.services.summary_service import build_context
from common.models.twin_opt_in import TwinOptIn

TWIN_SAMPLE_MESSAGE_LIMIT = 40  # свежесть-cap, отдельный от card_service.PORTRAIT_MESSAGE_LIMIT (50)
TWIN_FALLBACK_TEXT = "Двойник не смог ответить — попробуйте другую модель."


class TwinConsentError(Exception):
    """Цель не дала согласие (нет строки twin_opt_ins) или на паузе. Поднимается
    ДО любого чтения сообщений цели и ДО любого LLM-вызова (Critical Failure Mode #1)."""


async def _check_consent(session: AsyncSession, chat_id: int, target_user_id: int) -> None:
    status = (
        await session.execute(
            select(TwinOptIn.status).where(
                TwinOptIn.chat_id == chat_id, TwinOptIn.user_id == target_user_id
            )
        )
    ).scalar_one_or_none()
    if status != "active":
        raise TwinConsentError(f"user {target_user_id} has not opted in (status={status!r})")


async def build_twin_reply(
    session: AsyncSession, chat_id: int, target_user_id: int, target_display_name: str
) -> str:
    """Возвращает СЫРОЙ текст модели, БЕЗ дисклеймер-префикса (D-02 — префикс
    добавляет хендлер, не сервис)."""
    await _check_consent(session, chat_id, target_user_id)  # гейт ПЕРВОЙ строкой (Pitfall 5)

    # Блок 1: reuse существующего 14-дневного психо-портрета (D-03, без дублирующего SQL).
    portrait = await card_service.build_portrait(
        session, chat_id, target_user_id, target_display_name
    )
    # Блок 2: свежая raw-выборка сообщений, тот же char-budget, что у /summary.
    sample_rows = await card_service.fetch_user_recent_texts(
        session, chat_id, target_user_id, TWIN_SAMPLE_MESSAGE_LIMIT
    )
    char_budget = settings.ai_max_input_tokens * CHARS_PER_TOKEN
    sample_context = build_context(sample_rows, char_budget)

    system_prompt = (
        f"Ты — «Двойник» участника {target_display_name}. Его психологический портрет: "
        f"{portrait}\n\nЕго настоящие недавние сообщения (стиль, лексика, длина фраз):\n"
        f"{sample_context}\n\nНапиши ОДНУ короткую реплику в его стиле, 1-3 предложения. "
        "Не приписывай ему реальных фактов/обвинений на чувствительные темы."
    )
    model = await settings_service.get_active_model(session, chat_id)  # model-agnostic
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": "Обычная реплика в духе этого человека."},
    ]

    try:
        parts = [
            delta
            async for delta in ai_client.stream(
                messages, model=model, max_tokens=settings.twin_max_output_tokens
            )
        ]
    except RuntimeError:  # reasoning-only модель — деградация, не 500 (Pitfall 3)
        return TWIN_FALLBACK_TEXT
    return "".join(parts).strip() or TWIN_FALLBACK_TEXT


async def set_opt_in(session: AsyncSession, chat_id: int, user_id: int, status: str) -> None:
    """Upsert строки согласия (active/paused/declined) — используется
    /twin_optin, /twin_pause, /twin_resume и API-роуты /api/v1/twin/optin,
    /api/v1/twin/decline (onboarding-промпт miniapp). Пишет ТОЛЬКО переданный
    user_id (V4 — вызывающая сторона обязана передавать message.from_user.id/
    auth.user_id, не @arg)."""
    stmt = pg_insert(TwinOptIn).values(chat_id=chat_id, user_id=user_id, status=status)
    stmt = stmt.on_conflict_do_update(
        index_elements=["chat_id", "user_id"],
        set_={"status": stmt.excluded.status, "updated_at": func.now()},
    )
    await session.execute(stmt)


async def opt_out(session: AsyncSession, chat_id: int, user_id: int) -> bool:
    """Удаляет строку согласия (отсутствие строки = не подключён, Pattern 2).
    Возвращает True, если строка реально существовала и была удалена."""
    result = await session.execute(
        delete(TwinOptIn).where(TwinOptIn.chat_id == chat_id, TwinOptIn.user_id == user_id)
    )
    return result.rowcount > 0


async def get_status(session: AsyncSession, chat_id: int, user_id: int) -> str | None:
    """Текущий статус согласия вызывающего: 'active' | 'paused' | 'declined' |
    None (ещё ни разу не решал(а) — только это значение показывает
    onboarding-промпт miniapp, см. api/routes/twin.py::asked)."""
    return (
        await session.execute(
            select(TwinOptIn.status).where(
                TwinOptIn.chat_id == chat_id, TwinOptIn.user_id == user_id
            )
        )
    ).scalar_one_or_none()
