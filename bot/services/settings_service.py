"""KV-настройки чата (модель LLM, системный промпт) — AI-08.

get_setting/set_setting читают/пишут common.models.bot_setting.BotSetting и
держат in-process кэш значений (_cache) — чтобы ai_client (план 06) не делал
запрос к БД на каждый AI-вызов. set_setting инвалидирует (обновляет) кэш
немедленно после успешного execute — без окна устаревшего чтения между
записью и следующим get_setting в той же сессии/транзакции.

lru_cache здесь не подходит: значение зависит от переданного при вызове
session (per-request AsyncSession), и намеренная инвалидация "мгновенно
после записи" не композируется с decorator-кэшем — поэтому обычный dict.
"""

from __future__ import annotations

from sqlalchemy import func
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import settings
from common.models.bot_setting import BotSetting

KEY_MODEL = "ai_model"
KEY_PROMPT = "ai_system_prompt"

_cache: dict[str, str] = {}


async def get_setting(session: AsyncSession, chat_id: int, key: str, default: str) -> str:
    """Возвращает значение настройки: сперва из кэша, иначе из bot_settings.

    Отсутствующая запись -> default (кэшируется тоже, чтобы не бить БД
    повторными запросами до первого set_setting).
    """
    cache_key = f"{chat_id}:{key}"
    if cache_key in _cache:
        return _cache[cache_key]

    row = (
        await session.execute(
            select(BotSetting.value).where(BotSetting.chat_id == chat_id, BotSetting.key == key)
        )
    ).scalar_one_or_none()
    value = row if row is not None else default
    _cache[cache_key] = value
    return value


async def set_setting(
    session: AsyncSession, chat_id: int, key: str, value: str, updated_by_tg_id: int
) -> None:
    """Upsert настройки (pg_insert...on_conflict_do_update) + немедленная инвалидация кэша."""
    stmt = pg_insert(BotSetting).values(
        chat_id=chat_id,
        key=key,
        value=value,
        updated_by_tg_id=updated_by_tg_id,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["chat_id", "key"],
        set_={
            "value": stmt.excluded.value,
            "updated_by_tg_id": stmt.excluded.updated_by_tg_id,
            "updated_at": func.now(),
        },
    )
    await session.execute(stmt)
    _cache[f"{chat_id}:{key}"] = value  # без окна устаревшего чтения


async def get_active_model(session: AsyncSession, chat_id: int) -> str:
    """Активная модель для чата — фолбэк на settings.openai_model (env-дефолт)."""
    return await get_setting(session, chat_id, KEY_MODEL, settings.openai_model)


async def get_active_prompt(session: AsyncSession, chat_id: int) -> str:
    """Активный системный промпт для чата — фолбэк на settings.ai_default_system_prompt."""
    return await get_setting(session, chat_id, KEY_PROMPT, settings.ai_default_system_prompt)


def clear_cache() -> None:
    """Очистка in-process кэша — используется тестами для изоляции между кейсами."""
    _cache.clear()
