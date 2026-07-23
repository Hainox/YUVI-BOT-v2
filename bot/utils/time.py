"""Сериализация времени для API-ответов miniapp (запрошено пользователем
2026-07-23, "разные часовые пояса участников").

Все DateTime-колонки проекта — naive UTC (`datetime.utcnow()`/`func.now()`,
без `timezone=True`). Bare `datetime.isoformat()` такой колонки не несёт
оффсета ("2026-07-23T14:30:00"), а JS `new Date(...)` без 'Z'/оффсета в
ISO-строке парсит её как ЛОКАЛЬНОЕ время браузера, а не UTC — это ГАСИТ
автоматическую конвертацию в часовой пояс зрителя, которую браузер иначе
сделал бы сам через `toLocaleString()`/`toLocaleTimeString()`. Явный 'Z'
чинит это без какого-либо серверного хранения часового пояса пользователя —
конвертация происходит на клиенте, средствами самого браузера/ОС.
"""

from __future__ import annotations

from datetime import datetime


def to_utc_iso(dt: datetime) -> str:
    """ISO 8601 с явным 'Z' для naive UTC datetime — единственное место
    этого форматирования, переиспользуется везде, где сервис кладёт
    `created_at`/`closes_at`/`expires_at` в dict, уходящий клиенту JSON-ом."""
    return dt.isoformat() + "Z"
