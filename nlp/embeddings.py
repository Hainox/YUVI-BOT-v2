"""Локальные эмбеддинги предложений (768-dim) через sentence-transformers.

Совпадает по размерности с Vector(768) плана 02 (pgvector, message_embeddings).
Модель грузится один раз при импорте модуля (module-level singleton).
"""

from __future__ import annotations

import os

from sentence_transformers import SentenceTransformer

EMBEDDING_MODEL = os.environ.get(
    "NLP_EMBEDDING_MODEL", "sentence-transformers/paraphrase-multilingual-mpnet-base-v2"
)

_embedding_model = SentenceTransformer(EMBEDDING_MODEL)


def embed_texts(texts: list[str]) -> list[list[float]]:
    """Возвращает список 768-мерных нормализованных эмбеддингов по текстам."""
    if not texts:
        return []
    vectors = _embedding_model.encode(texts, normalize_embeddings=True)
    return vectors.tolist()
