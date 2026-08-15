"""Общие pytest-фикстуры для всего тестового набора.

Что здесь:
- `session` — асинхронная сессия SQLAlchemy к живому Postgres по паттерну
  "транзакция-на-тест": открываем соединение, начинаем транзакцию, отдаём
  AsyncSession, привязанную к этому соединению, а после теста делаем rollback.
  Так тесты не оставляют мусор в БД и не мешают друг другу.
- `bot` — AsyncMock вместо реального aiogram Bot, для тестов хендлеров без
  сетевых вызовов к Telegram.

Перед запуском:
- Нужен запущенный Postgres (см. docker compose up -d postgres) и переменная
  окружения DATABASE_URL, указывающая на него. Кредов здесь нет — только env.
"""

from __future__ import annotations

import os
from collections.abc import AsyncGenerator
from urllib.parse import urlparse
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy.ext.asyncio import create_async_engine


_ALLOWED_TEST_DB_HOSTS = {"127.0.0.1", "localhost"}
_COMPOSE_TEST_DB_HOSTS = {"postgres"}


def _assert_safe_test_database(database_url: str) -> None:
    """Не даёт случайно направить destructive-тесты в production.

    `postgres` разрешён только из явного Compose test-профиля, который
    выставляет `YUVI_TEST_ISOLATED=1`; обычный запуск принимает только
    localhost/127.0.0.1.
    """
    host = urlparse(database_url).hostname
    isolated_compose_run = os.environ.get("YUVI_TEST_ISOLATED") == "1"
    if host in _ALLOWED_TEST_DB_HOSTS:
        return
    if isolated_compose_run and host in _COMPOSE_TEST_DB_HOSTS:
        return
    pytest.exit(
        "DATABASE_URL host %r не похож на локальный/изолированный Postgres "
        "(ожидались %s). Установите YUVI_TEST_ISOLATED=1 только для "
        "изолированного Compose test-профиля; прогон остановлен до первого теста."
        % (host, sorted(_ALLOWED_TEST_DB_HOSTS)),
        returncode=2,
    )


@pytest_asyncio.fixture
async def session() -> AsyncGenerator[AsyncSession, None]:
    """Async-сессия Postgres на один тест, откатывается по завершении."""
    database_url = os.environ["DATABASE_URL"]
    _assert_safe_test_database(database_url)
    engine = create_async_engine(database_url, pool_pre_ping=True)

    async with engine.connect() as connection:
        transaction = await connection.begin()
        session_factory = async_sessionmaker(bind=connection, expire_on_commit=False)
        test_session = session_factory()
        try:
            yield test_session
        finally:
            await test_session.close()
            await transaction.rollback()

    await engine.dispose()


@pytest.fixture
def bot() -> AsyncMock:
    """AsyncMock вместо aiogram Bot — для тестов хендлеров без реального Telegram API."""
    return AsyncMock()
