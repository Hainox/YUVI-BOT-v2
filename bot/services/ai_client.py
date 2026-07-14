"""Singleton-клиент к OpenCode Go (OpenAI-совместимый) + стриминг ответа.

Модульный singleton AsyncOpenAI — один HTTP-клиент/пул соединений на процесс
бота, как и nlp_client.py делает для aiohttp (см. RESEARCH.md Pattern 1).

stream() отдаёт наружу ТОЛЬКО delta.content — reasoning-дельту (некоторые
модели каталога Go — DeepSeek/GLM — присылают "мысли" отдельным полем перед
финальным ответом) читаем защитно через getattr и НЕ отдаём вызывающему коду
(Pitfall 5): прямой доступ delta.reasoning_content уронил бы код на моделях/
версиях SDK, где такого атрибута нет вовсе.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator

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


async def stream(
    messages: list[dict],
    model: str,
    max_tokens: int,
) -> AsyncIterator[str]:
    """Стримит chat-completion от OpenCode Go, отдавая по частям только текст ответа.

    Если модель прислала только reasoning-дельты и ни одного символа
    content (модель "думала", но не ответила) — поднимаем RuntimeError с
    понятным русским текстом вместо тихого возврата пустой строки.
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
        raise RuntimeError("Модель вернула только reasoning без ответа — попробуйте другую модель")
