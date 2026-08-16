"""Локальные эмбеддинги BAAI/bge-m3 (1024 dim) через sentence-transformers.

Модель загружается один раз при импорте NLP-процесса; внешний LLM-провайдер
в этот шаг не используется.
"""

from __future__ import annotations

import os

from sentence_transformers import SentenceTransformer

EMBEDDING_MODEL = os.environ.get("NLP_EMBEDDING_MODEL", "BAAI/bge-m3")

_embedding_model = SentenceTransformer(EMBEDDING_MODEL)


def embed_texts(texts: list[str]) -> list[list[float]]:
    """Возвращает список нормализованных 1024-мерных эмбеддингов."""
    if not texts:
        return []
    vectors = _embedding_model.encode(texts, normalize_embeddings=True)
    return vectors.tolist()
