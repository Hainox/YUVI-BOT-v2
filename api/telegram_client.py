"""Raw-HTTP проверка членства/админства в чате без aiogram (D-01).

Семантически воспроизводит `bot/services/admin_service.py::is_chat_admin`
(тот же live-факт: статус ∈ {"administrator", "creator"} = админ, никакого
статичного allowlist) — процесс `api` не имеет aiogram `Bot`-объекта и не
должен его создавать ради двух REST-вызовов (RESEARCH.md Pattern 2).

Намеренное расхождение с бот-стороной (задокументировано по требованию D-01):
`get_chat_member_status` здесь кэшируется на `CACHE_TTL` секунд (in-memory,
под `asyncio.Lock`), тогда как `admin_service.is_chat_admin` делает live-запрос
на КАЖДЫЙ вызов без кэша. Причина — частота опроса из браузера (Mini App может
дёргать `/me` на каждый рендер экрана); окно устаревания ограничено `CACHE_TTL`
секунд (см. `bot/config.py::mini_app_membership_cache_ttl_sec`, default 300с).
Семантика "живой статус, не статичный allowlist" сохранена — кэшируется только
частота обновления, а не сам факт.

На не-200 ответе Telegram ИЛИ сетевой ошибке — fail-closed ("left"), не
поднимает исключение наверх (та же дисциплина "никогда не доверять
непроверенному праву доступа", что неявно есть в admin_service).
"""

from __future__ import annotations

import asyncio
import time

import httpx

from bot.config import settings

_cache: dict[tuple[int, int], tuple[float, str]] = {}
_lock = asyncio.Lock()
CACHE_TTL = settings.mini_app_membership_cache_ttl_sec


async def get_chat_member_status(
    client: httpx.AsyncClient, bot_token: str, chat_id: int, user_id: int
) -> str:
    """Возвращает статус участника чата (live, с TTL-кэшем `CACHE_TTL` секунд).

    Fail-closed: на не-200 ответе Telegram ИЛИ любой сетевой ошибке возвращает
    "left" вместо поднятия исключения — вызывающий (require_membership/
    require_admin) трактует "left" как отсутствие прав.
    """
    key = (chat_id, user_id)
    async with _lock:
        cached = _cache.get(key)
        if cached and time.monotonic() - cached[0] < CACHE_TTL:
            return cached[1]

    try:
        resp = await client.get(
            f"https://api.telegram.org/bot{bot_token}/getChatMember",
            params={"chat_id": chat_id, "user_id": user_id},
        )
    except Exception:
        return "left"  # fail-closed, но НЕ кэшируем — сетевой сбой не равен "вышел из чата"

    if resp.status_code != 200:
        return "left"  # то же: транзитная ошибка Telegram API не кэшируется

    status = resp.json()["result"]["status"]
    async with _lock:
        _cache[key] = (time.monotonic(), status)
    return status


def is_admin_status(status: str) -> bool:
    """True для 'administrator'/'creator' — та же семантика, что ADMINS в aiogram."""
    return status in ("administrator", "creator")


def reset_cache() -> None:
    """Очищает module-level кэш членства — для детерминированности тестов."""
    _cache.clear()
