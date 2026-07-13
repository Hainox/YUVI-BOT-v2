from __future__ import annotations

from os import getenv

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy.ext.asyncio import create_async_engine

DATABASE_URL = getenv("DATABASE_URL", "postgresql+asyncpg://yuvi:yuvi@localhost:5432/yuvi")

engine = create_async_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

