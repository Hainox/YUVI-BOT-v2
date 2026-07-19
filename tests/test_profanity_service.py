"""Юнит-тест bot/services/profanity_service.py (AWARDS-01).

Чистый юнит-тест, Postgres не нужен: count_profanity — CPU-трансформация
без БД, лемматизация идёт через реальный pymorphy3 MorphAnalyzer (singleton).
"""

from __future__ import annotations

from bot.services import profanity_service


def test_counts_inflected_forms():
    """Разные словоформы одной леммы («блядь», «бляди», «блядью») матчатся
    через лемматизацию, а не подстроку — все три должны засчитаться."""
    text = "ты блядь и бляди, блядью"
    assert profanity_service.count_profanity(text) >= 3


def test_clean_text_zero():
    """Обычный текст без мата даёт 0."""
    text = "привет, как твои дела сегодня"
    assert profanity_service.count_profanity(text) == 0


def test_empty_and_none_safe():
    """Пустая строка и None не должны падать — обе дают 0 (медиа-текст без caption)."""
    assert profanity_service.count_profanity("") == 0
    assert profanity_service.count_profanity(None) == 0


def test_singleton_not_rebuilt():
    """Два вызова count_profanity переиспользуют один и тот же MorphAnalyzer (Pitfall 6)."""
    profanity_service.count_profanity("тест один")
    analyzer_after_first_call = profanity_service._analyzer
    assert analyzer_after_first_call is not None

    profanity_service.count_profanity("тест два")
    analyzer_after_second_call = profanity_service._analyzer

    assert analyzer_after_first_call is analyzer_after_second_call
