"""Singleton-клиент к OpenCode Go (OpenAI-совместимый) + стриминг ответа.

Модульный singleton AsyncOpenAI — один HTTP-клиент/пул соединений на процесс
бота, как и nlp_client.py делает для aiohttp (см. RESEARCH.md Pattern 1).

stream() отдаёт наружу ТОЛЬКО delta.content — reasoning-дельту (некоторые
модели каталога Go — DeepSeek/GLM — присылают "мысли" отдельным полем перед
финальным ответом) читаем защитно через getattr и НЕ отдаём вызывающему коду
(Pitfall 5): прямой доступ delta.reasoning_content уронил бы код на моделях/
версиях SDK, где такого атрибута нет вовсе.

complete_with_fallback() — постоянная инфраструктура retry/fallback по
моделям (тех.долг из docs/roadmap.md, прототип жил только в разовом
диагностическом скрипте `chat_complaints_report.py` на ветке
scratch/chat-complaints-report, никогда не мерджился). Пробует primary_model,
затем остальной каталог settings.ai_available_models по порядку — но только
при ошибках, которые правдоподобно чинятся сменой модели (пустой reasoning-
ответ, таймаут/сеть/rate-limit/5xx/модель не найдена на гейтвее). Ошибки
авторизации/валидации запроса (одинаковые для любой модели этого же
гейтвея) НЕ перехватываются — ретраить их бессмысленно и это замаскировало
бы реальный баг вызывающего кода под "проблему модели".
"""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import AsyncIterator

import openai
from openai import AsyncOpenAI

from bot.config import settings

logger = logging.getLogger(__name__)

# AsyncOpenAI(api_key="") падает ещё на __init__ с OpenAIError("Missing
# credentials") — пустая строка трактуется SDK как отсутствие ключа. Без
# реального ключа бот не должен падать при старте (импорт этого модуля
# происходит для ЛЮБОГО хендлера через bot/main.py's _discover_routers, задолго
# до первого реального AI-вызова) — подставляем плейсхолдер, чтобы клиент
# сконструировался; настоящая ошибка авторизации всплывёт только при вызове
# .stream() (auth gate, а не краш при импорте).
if not settings.openai_api_key:
    logger.warning(
        "ai_client: OPENAI_API_KEY пуст — вызовы к OpenCode Go завершатся ошибкой авторизации"
    )

client = AsyncOpenAI(
    base_url=settings.openai_base_url,
    api_key=settings.openai_api_key or "sk-not-set",
    timeout=settings.ai_call_timeout_sec,
    max_retries=2,
)


class AIEmptyResponseError(RuntimeError):
    """Модель прислала только reasoning-дельты, ни одного символа content
    (запрошено 2026-07-24: `api/routes/shop.py` ловит это отдельно от общего
    `RuntimeError`, чтобы не глотать заодно и другие программные ошибки того
    же типа — тонкий, конкретный класс вместо голого `RuntimeError`).

    Несёт `model`, чтобы retry/fallback-инфраструктура и мониторинг могли
    атрибутировать отказ конкретной модели (запрошено 2026-07-28 вместе с
    complete_with_fallback — раньше исключение не несло вообще никаких полей)."""

    def __init__(self, model: str) -> None:
        self.model = model
        super().__init__(f"Модель {model} вернула только reasoning без ответа — попробуйте другую модель")


async def stream(
    messages: list[dict],
    model: str,
    max_tokens: int,
) -> AsyncIterator[str]:
    """Стримит chat-completion от OpenCode Go, отдавая по частям только текст ответа.

    Если модель прислала только reasoning-дельты и ни одного символа
    content (модель "думала", но не ответила) — поднимаем
    `AIEmptyResponseError` с понятным русским текстом вместо тихого возврата
    пустой строки.
    """
    saw_content = False
    saw_reasoning_only = False

    response = await client.chat.completions.create(
        model=model,
        messages=messages,
        stream=True,
        max_tokens=max_tokens,
        temperature=0,
    )
    async for chunk in response:
        if not chunk.choices:
            continue
        delta = chunk.choices[0].delta
        if delta.content:
            saw_content = True
            yield delta.content
        elif getattr(delta, "reasoning_content", None) or getattr(delta, "reasoning", None):
            saw_reasoning_only = True

    if not saw_content and saw_reasoning_only:
        raise AIEmptyResponseError(model)


# --- Retry/fallback по моделям (постоянная инфраструктура, D-Тех.долг) -------

# Ошибки, которые правдоподобно чинятся сменой модели на том же гейтвее:
# пустой reasoning-ответ, таймаут/сеть/rate-limit/5xx, модель не найдена.
# Сознательно НЕ включены AuthenticationError/PermissionDeniedError/
# BadRequestError (и прочие APIStatusError) — они одинаковы для любой модели
# этого же гейтвея, ретраить их бессмысленно и это спрятало бы реальный баг
# вызывающего кода (например, некорректно собранные messages) под видом
# "проблемы с моделью".
_FALLBACK_TRIGGERS: tuple[type[Exception], ...] = (
    AIEmptyResponseError,
    openai.APITimeoutError,
    openai.APIConnectionError,
    openai.RateLimitError,
    openai.InternalServerError,
    openai.NotFoundError,
)

_failure_counts: Counter[str] = Counter()


def available_models() -> list[str]:
    """Каталог моделей гейтвея из settings.ai_available_models (единственное
    место парсинга — было продублировано в bot/handlers/ai_admin.py)."""
    return [m.strip() for m in settings.ai_available_models.split(",") if m.strip()]


def _model_fallback_order(primary_model: str) -> list[str]:
    """primary_model первым, затем остальной каталог (без дублей), в порядке
    settings.ai_available_models — та же формула, что была в разовом
    diagnostic-скрипте chat_complaints_report.py (ветка
    scratch/chat-complaints-report, никогда не мерджилась)."""
    return [primary_model] + [m for m in available_models() if m != primary_model]


def get_failure_counts() -> dict[str, int]:
    """Число отказов (по _FALLBACK_TRIGGERS) на модель с начала процесса —
    лёгкий in-memory счётчик, не персистентный (сбрасывается при рестарте),
    без новой БД-таблицы/метрик-инфры (та же экономность, что у
    admin_analytics_service — "no new tracking infra"). Используется
    /model_health (bot/handlers/ai_admin.py)."""
    return dict(_failure_counts)


async def complete_with_fallback(
    messages: list[dict],
    primary_model: str,
    max_tokens: int,
) -> str:
    """Буферизует полный ответ (не построчный стриминг — решение, годно ли
    попробовать другую модель, можно принять только ПОСЛЕ того, как весь
    ответ собран и видно, что он пуст) и возвращает строку.

    Пробует primary_model, при отказе из _FALLBACK_TRIGGERS — следующую
    модель из settings.ai_available_models, и так по кругу, пока одна из
    попыток не соберёт непустой ответ. Если отказали ВСЕ модели каталога —
    поднимает последнюю поймавшуюся ошибку (не глотает её).

    НЕ подходит для случаев живого построчного стриминга в Telegram-сообщение
    (см. summary_service.stream_summary, единственный такой случай в проекте
    на 2026-07-28) — там частичный текст уже показан пользователю к моменту,
    когда стало ясно, что ответ пуст, откатывать нечего."""
    last_exc: Exception | None = None
    for attempt, model in enumerate(_model_fallback_order(primary_model)):
        try:
            parts = [delta async for delta in stream(messages, model=model, max_tokens=max_tokens)]
            return "".join(parts)
        except _FALLBACK_TRIGGERS as exc:
            _failure_counts[model] += 1
            logger.warning(
                "ai_client.complete_with_fallback: модель=%s попытка=%d тип_ошибки=%s — пробую следующую",
                model,
                attempt,
                type(exc).__name__,
            )
            last_exc = exc
            continue

    assert last_exc is not None  # _model_fallback_order всегда непустой (минимум primary_model)
    raise last_exc
