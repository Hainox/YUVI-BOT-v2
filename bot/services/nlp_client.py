"""HTTP-клиент к отдельному NLP-контейнеру (stateless, только классификация/эмбеддинги).

nlp остаётся stateless: всю запись в БД делает бот (NLP-02, RESEARCH.md Anti-Patterns).
Этот модуль НЕ импортирует SQLAlchemy/модели — только aiohttp-запросы к
settings.nlp_service_url.

Используем aiohttp (уже транзитивная зависимость aiogram) — новый HTTP-пакет
не добавляем.

Ретрай с экспоненциальным бэкоффом покрывает холодный старт nlp-контейнера
(RESEARCH.md Pitfall 7 — загрузка моделей transformers/sentence-transformers на
CPU может занимать 10-60+ секунд).
"""

from __future__ import annotations

import asyncio
import logging

import aiohttp

from bot.config import settings

logger = logging.getLogger(__name__)


async def _post_with_retry(
    path: str,
    payload: dict,
    retries: int = 5,
    base_delay: float = 2.0,
) -> dict:
    """POST {settings.nlp_service_url}{path} с ретраем на ошибках соединения/таймаута.

    Экспоненциальный бэкофф (base_delay * 2**попытка) — покрывает холодный
    старт nlp (Pitfall 7). После исчерпания попыток исключение пробрасывается
    вызывающему коду.
    """
    url = f"{settings.nlp_service_url}{path}"
    timeout = aiohttp.ClientTimeout(total=settings.ai_call_timeout_sec)

    last_exc: Exception | None = None
    for attempt in range(retries):
        try:
            async with aiohttp.ClientSession(timeout=timeout) as http_session:
                async with http_session.post(url, json=payload) as response:
                    response.raise_for_status()
                    return await response.json()
        except (aiohttp.ClientConnectorError, asyncio.TimeoutError, aiohttp.ServerTimeoutError) as exc:
            last_exc = exc
            delay = base_delay * (2**attempt)
            logger.warning(
                "nlp_client: попытка %s/%s к %s не удалась (%s), повтор через %.1fс",
                attempt + 1,
                retries,
                url,
                exc,
                delay,
            )
            if attempt < retries - 1:
                await asyncio.sleep(delay)

    assert last_exc is not None
    raise last_exc


async def classify_batch(texts: list[str]) -> list[dict]:
    """POST /classify/batch — возвращает список {sentiment_label, sentiment_score, toxicity_score}."""
    response = await _post_with_retry("/classify/batch", {"texts": texts})
    return response["results"]


async def embed_batch(texts: list[str]) -> list[list[float]]:
    """POST /embed/batch — возвращает список 768-мерных эмбеддингов."""
    response = await _post_with_retry("/embed/batch", {"texts": texts})
    return response["embeddings"]
