"""Смоук-скрипт: api-контейнер реально открывает сессию БД (Success Criterion 5).

Что делает:
    Открывает асинхронную сессию SQLAlchemy через common.db.session.SessionLocal
    (тот же движок, что использует api/), выполняет SELECT 1 и печатает
    результат. Доказывает, что api-образ (yuvi-api:py311) может не только
    ИМПОРТИРОВАТЬ sqlalchemy/asyncpg, но и реально подключиться к Postgres.

Когда использовать:
    После пересборки api-образа — прогнать внутри контейнера на сети
    yuvibotv2_default, чтобы убедиться, что api может открыть соединение к БД
    (в api-образе нет pytest, только пакеты из api/requirements.txt).

Что нужно настроить перед запуском:
    DATABASE_URL — переменная окружения, указывающая на живой Postgres
    (например postgresql+asyncpg://yuvi:yuvi@postgres:5432/yuvi).
    BOT_TOKEN/CHAT_ID — читаются bot.config.settings при импорте
    common.db.session, должны быть заданы (даже фиктивным значением) для
    прохождения валидации Settings.

Запуск:
    docker run --rm --network yuvibotv2_default \
        -e DATABASE_URL=... -e BOT_TOKEN=test -e CHAT_ID=-100 \
        yuvi-api:py311 python scripts/api_db_smoke.py
"""

from __future__ import annotations

import asyncio

from sqlalchemy import text

from common.db.session import SessionLocal


async def main() -> None:
    async with SessionLocal() as session:
        result = await session.execute(text("SELECT 1"))
        assert result.scalar_one() == 1
    print("api opened DB session ok")


if __name__ == "__main__":
    asyncio.run(main())
