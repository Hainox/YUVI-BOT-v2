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
`appid` (без человекочитаемого имени). Имя резолвится (запрошено
пользователем 2026-07-23, после первой живой проверки секретов) через
публичный `store.steampowered.com/api/appdetails` — ВТОРОЙ внешний
HTTP-хост, сознательно за пределами исходной единственной границы доверия
`api.steampowered.com` (threat_model плана 05-06), но тоже официальный
Valve-хост, без креда в запросе (только appid — наши собственные данные,
не пользовательский ввод, SSRF не аргумент) и с тем же graceful-degradation
контрактом (D-11): любой сбой резолва → `Steam App #{appid}`, никогда
исключение наружу. Резолвится ТОЛЬКО для уже выбранного дня item'а (не для
всего wishlist) — один HTTP-запрос в день, не N.

TTL-кэш wishlist-списка (module-level, 6ч) — не дёргает Wishlist API на
каждый `/awards`. Отдельный module-level dict-кэш `_name_cache` (appid ->
имя, без TTL — имя игры не меняется) — повторный `/awards` в тот же день с
той же выбранной игрой не бьёт по appdetails повторно.
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
_APPDETAILS_URL = "https://store.steampowered.com/api/appdetails"
_CACHE_TTL_SEC = 6 * 3600

_cache_items: list[dict] | None = None
_cache_fetched_at: float = 0.0
_name_cache: dict[int, str] = {}


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


async def _fetch_appdetails_json(appid: int) -> dict:
    """GET публичного store/appdetails (форма `_fetch_wishlist_json` —
    отдельная функция специально ради monkeypatch в тестах, без реального
    aiohttp)."""
    timeout = aiohttp.ClientTimeout(total=settings.ai_call_timeout_sec)
    async with aiohttp.ClientSession(timeout=timeout) as http_session:
        async with http_session.get(
            _APPDETAILS_URL, params={"appids": str(appid), "l": "russian"}
        ) as response:
            response.raise_for_status()
            return await response.json()


async def _resolve_app_name(appid: int) -> str:
    """Резолвит человекочитаемое имя по appid через публичный
    store/appdetails (не требует STEAM_API_KEY — отдельный публичный
    эндпоинт). Кэш без TTL (имя игры не меняется) — module-level
    `_name_cache`. Любой сбой (сеть/форма ответа/success=False) деградирует
    в `Steam App #{appid}`, тот же D-11 контракт, что у остального модуля —
    resolve-провал НЕ должен превращать уже найденную игру в «Steam
    недоступен»."""
    if appid in _name_cache:
        return _name_cache[appid]

    fallback = f"Steam App #{appid}"
    try:
        data = await _fetch_appdetails_json(appid)
        entry = data.get(str(appid)) or {}
        name = entry.get("data", {}).get("name") if entry.get("success") else None
    except Exception:  # noqa: BLE001 - graceful degradation, тот же D-11, что остальной модуль
        logger.warning("steam_service: не удалось резолвнуть имя appid=%s", appid, exc_info=True)
        return fallback

    if not name:
        return fallback
    _name_cache[appid] = name
    return name


async def get_random_wishlist_game(day_msk: date) -> str | None:
    """Название (или `Steam App #{appid}`) случайной игры из Steam Wishlist,
    детерминированной по MSK-дню (Pitfall 5). `None`, если
    `STEAM_API_KEY`/`STEAM_ID64` не заданы (без единого HTTP-запроса) или
    любая ошибка API/пустой список (graceful-degradation, D-11/T-05-14)."""
    if not settings.steam_api_key or not settings.steam_id64:
        return None

    # WR-03 (05-REVIEW.md): раньше только _get_wishlist_items() был под
    # try/except — сортировка/форматирование ниже могли бросить необработанный
    # AttributeError (например, позиции wishlist не той формы/схемы), который
    # уходил из get_random_wishlist_game ДО awards_service.run_awards'ного
    # session.commit() и тихо откатывал уже посчитанные (внутри ещё не
    # закоммиченных SAVEPOINT) выплаты 6 номинаций за день. Оборачиваем ВЕСЬ
    # путь — сеть + разбор ответа — одним try/except: любой сбой формы ответа
    # деградирует в None (уже установленный fallback «Steam недоступен»,
    # D-11), а не пробрасывается мимо границы транзакции.
    try:
        items = await _get_wishlist_items()
        if not items:
            return None

        # Сортировка по appid ДО выбора — детерминизм зависит ТОЛЬКО от day_msk,
        # не от недокументированного порядка ответа Steam API (см. модульный docstring).
        sorted_items = sorted(items, key=lambda item: item.get("appid", 0))
        chosen = random.Random(day_msk.toordinal()).choice(sorted_items)
        appid = chosen.get("appid")
    except Exception:  # noqa: BLE001 - любая сетевая/HTTP/форма-ответа ошибка graceful (D-11)
        logger.warning("steam_service: не удалось получить/разобрать Steam Wishlist", exc_info=True)
        return None

    # Wishlist API иногда сам отдаёт "name" — тогда appdetails не нужен
    # вообще (0 доп. HTTP-запросов). Иначе резолвим ТОЛЬКО выбранный appid
    # (не весь wishlist) через _resolve_app_name.
    inline_name = chosen.get("name")
    if inline_name:
        return inline_name
    if appid is None:
        return "Steam App #?"
    return await _resolve_app_name(appid)
