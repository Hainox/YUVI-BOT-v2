"""Общий conftest для тестов nlp-контейнера.

Лёгкий набор: nlp — stateless HTTP-сервис без БД, поэтому здесь нет фикстур
Postgres/AsyncSession (в отличие от bot/tests/conftest.py). Тесты просто
импортируют модули nlp/*.py напрямую и проверяют инференс-функции.
"""

from __future__ import annotations
