"""Тесты nlp/toxicity.py — проверка диапазонов score, чтобы поймать перепутанные
softmax/sigmoid головы (RESEARCH.md Pitfall 2: "score сидит в 0.2-0.4 вместо >0.8"
— явный признак ошибки)."""

from __future__ import annotations

from nlp.toxicity import toxicity_scores


def test_toxicity_scores_toxic_phrase() -> None:
    scores = toxicity_scores(["Ты полный придурок и мразь, иди на хуй!"])
    assert len(scores) == 1
    assert scores[0] > 0.8


def test_toxicity_scores_neutral_phrase() -> None:
    scores = toxicity_scores(["Сегодня хорошая погода, давай сходим гулять в парк."])
    assert len(scores) == 1
    assert scores[0] < 0.3


def test_toxicity_scores_empty_list() -> None:
    assert toxicity_scores([]) == []


def test_toxicity_scores_batch_range() -> None:
    scores = toxicity_scores(["Привет, как дела?", "Ненавижу тебя, урод!"])
    assert len(scores) == 2
    for score in scores:
        assert 0.0 <= score <= 1.0
