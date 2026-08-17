"""Локальные эмбеддинги BAAI/bge-m3 (1024 dim) через sentence-transformers.

Модель загружается один раз при импорте NLP-процесса; внешний LLM-провайдер
в этот шаг не используется.
"""

from __future__ import annotations

import asyncio
import os

from sentence_transformers import SentenceTransformer

EMBEDDING_MODEL = os.environ.get("NLP_EMBEDDING_MODEL", "BAAI/bge-m3")

_embedding_model = SentenceTransformer(EMBEDDING_MODEL)


async def embed_texts(texts: list[str]) -> list[list[float]]:
    """Возвращает список нормализованных 1024-мерных эмбеддингов.

    Асинхронна: sentence-transformers encode() — синронный CPU-стадий,
    оборачиваем в asyncio.to_thread, чтобы не блокировать event loop uvicorn
    (иначе health-checks и /q-запросы ждут за embed_worker батчом из 128 текстов).
    """
    if not texts:
        return []
    vectors = await asyncio.to_thread(_embedding_model.encode, texts, normalize_embeddings=True)
    return vectors.tolist()
