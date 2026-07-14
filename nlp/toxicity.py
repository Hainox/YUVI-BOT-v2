"""Toxicity-классификация русского текста (multilabel, sigmoid).

ВАЖНО (RESEARCH.md Pattern 4 / Pitfall 2): cointegrated/rubert-tiny-toxicity —
мультилейбл-модель с 5 независимыми метками [non-toxic, insult, obscenity,
threat, dangerous]. НЕЛЬЗЯ использовать transformers.pipeline() по умолчанию
(он ожидает softmax/мультикласс) — нужен сырой torch.sigmoid по логитам,
иначе получаются правдоподобные, но неверные значения.
"""

from __future__ import annotations

import os

import torch
from transformers import AutoModelForSequenceClassification, AutoTokenizer

TOXICITY_MODEL = os.environ.get(
    "NLP_TOXICITY_MODEL", "cointegrated/rubert-tiny-toxicity"
)

# Module-level singletons — модель и токенизатор грузятся один раз при импорте.
_tox_tokenizer = AutoTokenizer.from_pretrained(TOXICITY_MODEL)
_tox_model = AutoModelForSequenceClassification.from_pretrained(TOXICITY_MODEL)
_tox_model.eval()


def toxicity_scores(texts: list[str]) -> list[float]:
    """Возвращает агрегированный "is bad" score (0..1) по каждому тексту.

    Формула — из карточки модели: 1 - proba[:, 0] * (1 - proba[:, -1]),
    где proba[:, 0] = non-toxic, proba[:, -1] = dangerous.
    """
    if not texts:
        return []
    inputs = _tox_tokenizer(texts, return_tensors="pt", truncation=True, padding=True)
    with torch.no_grad():
        logits = _tox_model(**inputs).logits
        proba = torch.sigmoid(logits).numpy()
    scores = 1 - proba[:, 0] * (1 - proba[:, -1])
    return [float(score) for score in scores]
