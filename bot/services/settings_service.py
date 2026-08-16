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

from bot.config import EXPLICIT_LANGUAGE_INSTRUCTION
from bot.config import settings
from common.models.bot_setting import BotSetting

KEY_MODEL = "ai_model"
KEY_PROMPT = "ai_system_prompt"
KEY_EXPLICIT_LANGUAGE = "ai_allow_explicit_language"

# Sentinel "нет строки BotSetting" — ОТДЕЛЬНЫЙ от любого default (Pitfall,
# найдено 2026-07-28): get_active_model теперь вызывается для ОДНОГО и того же
# (chat_id, KEY_MODEL) с РАЗНЫМИ default (q_service vs остальные сервисы,
# см. ai_structured_model в bot/config.py). Если кэшировать уже подставленный
# default, первый вызов для чата "залипает" в кэше навсегда — второй вызов с
# другим default для того же чата получил бы ЧУЖОЙ закэшированный фолбэк
# вместо своего. Кэш обязан хранить факт "есть ли override" отдельно от
# default, который каждый вызывающий передаёт заново.
_MISSING = object()

_cache: dict[str, str | object] = {}


async def get_setting(session: AsyncSession, chat_id: int, key: str, default: str) -> str:
    """Возвращает значение настройки: сперва из кэша, иначе из bot_settings.

    Отсутствующая запись -> default. Кэшируется РЕЗУЛЬТАТ ЗАПРОСА В БД
    (значение строки или _MISSING, если строки нет) — НЕ подставленный default,
    иначе повторный вызов с другим default для того же (chat_id, key) вернул бы
    default из ПЕРВОГО вызова (см. комментарий у _MISSING).
    """
    cache_key = f"{chat_id}:{key}"
    if cache_key in _cache:
        cached = _cache[cache_key]
        return default if cached is _MISSING else cached

    row = (
        await session.execute(
            select(BotSetting.value).where(BotSetting.chat_id == chat_id, BotSetting.key == key)
        )
    ).scalar_one_or_none()
    _cache[cache_key] = row if row is not None else _MISSING
    return row if row is not None else default


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


async def get_active_model(session: AsyncSession, chat_id: int, default: str | None = None) -> str:
    """Активная модель для чата — фолбэк на явно переданный `default`, иначе
    на settings.openai_model (env-дефолт, historically twin-специфичный —
    см. bot/config.py). `default` позволяет вызывающему сервису задать СВОЙ
    фолбэк (найдено 2026-07-28: kimi-k2.6, дефолт openai_model, систематически
    падает AIEmptyResponseError на промптах со строгим форматом — ask/card/
    digest/summary/topics/phrase/joke/social/lurker передают
    default=settings.ai_structured_model, q_service передаёт свой дефолт
    AI_Q_MODEL). Override через /model_set — ОДИН общий
    ключ (KEY_MODEL) на чат вне зависимости от default: явный выбор админа
    сознательно перекрывает оба "направления" сразу, разделение — только в
    фолбэке по умолчанию."""
    return await get_setting(
        session, chat_id, KEY_MODEL, default if default is not None else settings.openai_model
    )


async def get_explicit_language_enabled(session: AsyncSession, chat_id: int) -> bool:
    """Возвращает explicit-режим чата с env-дефолтом."""
    default = "1" if settings.ai_allow_explicit_language else "0"
    value = await get_setting(session, chat_id, KEY_EXPLICIT_LANGUAGE, default)
    return value == "1"


async def set_explicit_language(
    session: AsyncSession, chat_id: int, enabled: bool, updated_by_tg_id: int
) -> None:
    """Переключает explicit-режим конкретного чата без перезапуска бота."""
    await set_setting(
        session,
        chat_id,
        KEY_EXPLICIT_LANGUAGE,
        "1" if enabled else "0",
        updated_by_tg_id,
    )


async def get_active_prompt(session: AsyncSession, chat_id: int) -> str:
    """Активный системный промпт плюс политика языка AI-ответов чата."""
    prompt = await get_setting(session, chat_id, KEY_PROMPT, settings.ai_default_system_prompt)
    if await get_explicit_language_enabled(session, chat_id):
        return f"{prompt}\n\n{EXPLICIT_LANGUAGE_INSTRUCTION}"
    return prompt


def clear_cache() -> None:
    """Очистка in-process кэша — используется тестами для изоляции между кейсами."""
    _cache.clear()
