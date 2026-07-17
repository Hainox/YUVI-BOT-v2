from __future__ import annotations

import asyncio
from logging.config import fileConfig
from os import getenv

from alembic import context
from sqlalchemy import pool
from sqlalchemy.ext.asyncio import async_engine_from_config

from common.db.base import Base
from common.models import bet  # noqa: F401
from common.models import bot_setting  # noqa: F401
from common.models import casino_game  # noqa: F401
from common.models import chat_bank  # noqa: F401
from common.models import clicker_farm  # noqa: F401
from common.models import clicker_market_pool  # noqa: F401
from common.models import clicker_market_price  # noqa: F401
from common.models import daily_stat  # noqa: F401
from common.models import duel  # noqa: F401
from common.models import economy_tx  # noqa: F401
from common.models import emoji_frequency  # noqa: F401
from common.models import feedback  # noqa: F401
from common.models import gacha_collection  # noqa: F401
from common.models import market  # noqa: F401
from common.models import message  # noqa: F401
from common.models import message_edit  # noqa: F401
from common.models import message_embedding  # noqa: F401
from common.models import reaction  # noqa: F401
from common.models import user  # noqa: F401
from common.models import user_balance  # noqa: F401
from common.models import word_frequency  # noqa: F401

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

database_url = getenv("DATABASE_URL", "")
if database_url:
    config.set_main_option("sqlalchemy.url", database_url)


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(url=url, target_metadata=target_metadata, literal_binds=True)

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)

    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
