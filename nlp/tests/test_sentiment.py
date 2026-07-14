"""Тесты nlp/sentiment.py — проверка диапазонов, а не только "без исключения"
(RESEARCH.md Pitfall 2)."""

from __future__ import annotations

from nlp.sentiment import classify_sentiment


def test_classify_sentiment_positive_phrase() -> None:
    result = classify_sentiment(["Сегодня прекрасный день, я очень счастлив!"])
    assert len(result) == 1
    assert result[0]["sentiment_label"] == "positive"
    assert 0.0 <= result[0]["sentiment_score"] <= 1.0


def test_classify_sentiment_negative_phrase() -> None:
    result = classify_sentiment(["Это ужасно, всё плохо, я в отчаянии."])
    assert len(result) == 1
    assert result[0]["sentiment_label"] == "negative"
    assert 0.0 <= result[0]["sentiment_score"] <= 1.0


def test_classify_sentiment_empty_list() -> None:
    assert classify_sentiment([]) == []


def test_classify_sentiment_batch() -> None:
    result = classify_sentiment(["Отличная новость!", "Кошмар, всё сломалось."])
    assert len(result) == 2
    for item in result:
        assert "sentiment_label" in item
        assert "sentiment_score" in item
