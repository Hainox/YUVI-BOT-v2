"""Sentiment-классификация русского текста (multiclass, softmax).

Метки: neutral | positive | negative. Модель грузится один раз при импорте
модуля (module-level singleton) — холодный старт покрыт HEALTHCHECK'ом
контейнера (nlp/Dockerfile).
"""

from __future__ import annotations

import os

from transformers import pipeline

SENTIMENT_MODEL = os.environ.get(
    "NLP_SENTIMENT_MODEL", "seara/rubert-tiny2-russian-sentiment"
)
# Без явного truncation/max_length pipeline() не обрезает вход — длинное
# сообщение (пример из живого бэкфилла: 2173 токена) падает в forward()
# модели с RuntimeError на несовпадении размера position embeddings.
# 512 — консервативный предел, безопасный для любых rubert-tiny-вариантов.
NLP_MAX_LENGTH = int(os.environ.get("NLP_MAX_LENGTH", "512"))

# transformers.pipeline() для text-classification применяет softmax под капотом —
# это корректно для этой модели (мультикласс: neutral/positive/negative).
_sentiment_pipe = pipeline("text-classification", model=SENTIMENT_MODEL, device=-1)


def classify_sentiment(texts: list[str]) -> list[dict]:
    """Возвращает список {"sentiment_label": str, "sentiment_score": float} по текстам."""
    if not texts:
        return []
    raw_results = _sentiment_pipe(texts, truncation=True, max_length=NLP_MAX_LENGTH)
    return [
        {"sentiment_label": item["label"], "sentiment_score": float(item["score"])}
        for item in raw_results
    ]
