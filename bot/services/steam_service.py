"""Steam Wishlist «игра дня» (AWARDS-01, инфо-номинация `/awards`) — официальный
Steam Web API `IWishlistService/GetWishlist`, а НЕ сломанный unauthenticated
скрейпинг `store.steampowered.com/wishlist/.../wishlistdata/` (05-RESEARCH.md
Pitfall 7 — старый эндпоинт де-факто сломан, 302→HTML вместо JSON).

Используем aiohttp (уже транзитивная зависимость aiogram, как и в
`external_markets.py`) — новый HTTP-клиент не добавляем.

Graceful-degradation (D-11): нет `STEAM_API_KEY`/`STEAM_ID64` ИЛИ любая
ошибка HTTP/пустой ответ → `None` без исключения наружу — `/awards`
показывает «Steam недоступен 😢» вместо падения команды.

Идемпотентность (Pitfall 5 — "idempotent-per-day не значит deterministic для
рандомных пиков"): выбор игры детерминирован по MSK-дню
(`random.Random(day_msk.toordinal())`), а список позиций wishlist СНАЧАЛА
сортируется по `appid` — официальный Steam API не документирует стабильный
порядок ответа между вызовами, поэтому сортировка перед выбором делает
результат детерминированным ИСКЛЮЧИТЕЛЬНО от `day_msk`, а не от порядка
ответа сервера. `awards_service` намеренно НЕ заводит для Steam-игры
отдельную строку в `daily_picks` (см. `awards_service` docstring — модуль
самодостаточен, идемпотентность выплат обеспечивает `ref_id`); day-seeded
детерминизм здесь — единственный источник стабильности повторного
`/awards` в тот же день, поэтому сортировка по `appid` обязательна, а не
опциональная подстраховка.

Реальный ответ `IWishlistService/GetWishlist/v1` отдаёт позиции только с
`appid` (без человекочитаемого имени) — этот модуль ожидает опциональное
поле `name` у каждой позиции и иначе отдаёт `Steam App #{appid}`. Резолв
реальных названий через отдельный эндпоинт (`appdetails`/store API) —
второй внешний HTTP-хост, вне единственной границы доверия `api.
steampowered.com`, зафиксированной в threat_model плана 05-06; сознательно
не добавляется в этом плане (реальная Steam-проверка остаётся manual-only
до заполнения секретов, D-11).

TTL-кэш (module-level, 6ч) — не дёргает Steam API на каждый `/awards`.
"""

from __future__ import annotations

import logging
import random
import time
from datetime import date

import aiohttp

from bot.config import settings

logger = logging.getLogger(__name__)

_WISHLIST_URL = "https://api.steampowered.com/IWishlistService/GetWishlist/v1/"
_CACHE_TTL_SEC = 6 * 3600

_cache_items: list[dict] | None = None
_cache_fetched_at: float = 0.0


async def _fetch_wishlist_json(key: str, steamid: str) -> dict:
    """GET официального Steam Web API. `key`/`steamid` — только из
    `settings.steam_api_key`/`settings.steam_id64` (.env, D-11), никогда не
    логируются (T-05-05)."""
    timeout = aiohttp.ClientTimeout(total=settings.ai_call_timeout_sec)
    async with aiohttp.ClientSession(timeout=timeout) as http_session:
        async with http_session.get(
            _WISHLIST_URL, params={"key": key, "steamid": steamid}
        ) as response:
            response.raise_for_status()
            return await response.json()


async def _get_wishlist_items() -> list[dict]:
    """TTL-кэшированный список позиций wishlist. Может бросить исключение —
    ловится вызывающим `get_random_wishlist_game` (graceful None, D-11)."""
    global _cache_items, _cache_fetched_at
    now = time.monotonic()
    if _cache_items is not None and (now - _cache_fetched_at) < _CACHE_TTL_SEC:
        return _cache_items

    data = await _fetch_wishlist_json(settings.steam_api_key, settings.steam_id64)
    items = data.get("response", {}).get("items", []) or []
    _cache_items = items
    _cache_fetched_at = now
    return items


async def get_random_wishlist_game(day_msk: date) -> str | None:
    """Название (или `Steam App #{appid}`) случайной игры из Steam Wishlist,
    детерминированной по MSK-дню (Pitfall 5). `None`, если
    `STEAM_API_KEY`/`STEAM_ID64` не заданы (без единого HTTP-запроса) или
    любая ошибка API/пустой список (graceful-degradation, D-11/T-05-14)."""
    if not settings.steam_api_key or not settings.steam_id64:
        return None

    try:
        items = await _get_wishlist_items()
    except Exception:  # noqa: BLE001 - любая сетевая/HTTP-ошибка graceful (D-11)
        logger.warning("steam_service: не удалось получить Steam Wishlist", exc_info=True)
        return None

    if not items:
        return None

    # Сортировка по appid ДО выбора — детерминизм зависит ТОЛЬКО от day_msk,
    # не от недокументированного порядка ответа Steam API (см. модульный docstring).
    sorted_items = sorted(items, key=lambda item: item.get("appid", 0))
    names = [item.get("name") or f"Steam App #{item.get('appid')}" for item in sorted_items]
    return random.Random(day_msk.toordinal()).choice(names)
