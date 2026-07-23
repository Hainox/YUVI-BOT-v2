"""Unit-тесты bot.services.steam_service — без реальной сети и без Postgres.

Реальный HTTP полностью замокан (monkeypatch на `steam_service._fetch_wishlist_json`,
по образцу tests/test_external_markets.py, где так же мокается `_get_json`).

Ключевые кейсы:
- Нет STEAM_API_KEY/STEAM_ID64 → None БЕЗ похода в HTTP (D-11 graceful-degradation).
- Один и тот же MSK-день → одна и та же игра (идемпотентность, Pitfall 5) —
  список игр в моке отсортирован по appid ДО выбора, поэтому результат не
  зависит от порядка ответа API, только от day_msk.
- Любая ошибка HTTP (исключение из aiohttp) → None, без исключения наружу.
"""

from __future__ import annotations

from datetime import date
from unittest.mock import AsyncMock

import pytest

from bot.config import settings
from bot.services import steam_service


def _reset_cache(monkeypatch) -> None:
    """Кэш module-level — сбрасываем перед каждым тестом, чтобы тесты не
    зависели от порядка выполнения друг друга."""
    monkeypatch.setattr(steam_service, "_cache_items", None)
    monkeypatch.setattr(steam_service, "_cache_fetched_at", 0.0)
    monkeypatch.setattr(steam_service, "_name_cache", {})


def _set_steam_settings(monkeypatch, key: str = "fake-key", steamid: str = "fake-id") -> None:
    monkeypatch.setattr(settings, "steam_api_key", key)
    monkeypatch.setattr(settings, "steam_id64", steamid)


@pytest.mark.asyncio
async def test_no_key_returns_none(monkeypatch):
    _reset_cache(monkeypatch)
    _set_steam_settings(monkeypatch, key="", steamid="")
    mock_fetch = AsyncMock()
    monkeypatch.setattr(steam_service, "_fetch_wishlist_json", mock_fetch)

    result = await steam_service.get_random_wishlist_game(date(2026, 7, 19))

    assert result is None
    mock_fetch.assert_not_awaited()


@pytest.mark.asyncio
async def test_day_deterministic_pick(monkeypatch):
    _reset_cache(monkeypatch)
    _set_steam_settings(monkeypatch)

    games = {
        "response": {
            "items": [
                {"appid": 30, "name": "Игра C"},
                {"appid": 10, "name": "Игра A"},
                {"appid": 20, "name": "Игра B"},
            ]
        }
    }
    monkeypatch.setattr(steam_service, "_fetch_wishlist_json", AsyncMock(return_value=games))

    day = date(2026, 7, 19)
    first = await steam_service.get_random_wishlist_game(day)
    second = await steam_service.get_random_wishlist_game(day)

    assert first is not None
    assert first == second
    assert first in {"Игра A", "Игра B", "Игра C"}


@pytest.mark.asyncio
async def test_different_days_can_pick_different_games(monkeypatch):
    """Не строгая гарантия различия (могло совпасть случайно), но
    детерминированный выбор день1 != обязательно день2 — проверяем как
    минимум, что функция не падает и возвращает валидный результат для
    другого дня с той же кэшированной выборкой."""
    _reset_cache(monkeypatch)
    _set_steam_settings(monkeypatch)

    games = {
        "response": {
            "items": [
                {"appid": 30, "name": "Игра C"},
                {"appid": 10, "name": "Игра A"},
                {"appid": 20, "name": "Игра B"},
            ]
        }
    }
    monkeypatch.setattr(steam_service, "_fetch_wishlist_json", AsyncMock(return_value=games))

    result_day1 = await steam_service.get_random_wishlist_game(date(2026, 7, 19))
    result_day2 = await steam_service.get_random_wishlist_game(date(2026, 7, 20))

    assert result_day1 in {"Игра A", "Игра B", "Игра C"}
    assert result_day2 in {"Игра A", "Игра B", "Игра C"}


@pytest.mark.asyncio
async def test_http_error_graceful_none(monkeypatch):
    _reset_cache(monkeypatch)
    _set_steam_settings(monkeypatch)
    monkeypatch.setattr(
        steam_service, "_fetch_wishlist_json", AsyncMock(side_effect=Exception("boom"))
    )

    result = await steam_service.get_random_wishlist_game(date(2026, 7, 19))

    assert result is None


@pytest.mark.asyncio
async def test_malformed_item_shape_graceful_none(monkeypatch):
    """WR-03 (05-REVIEW.md): раньше только сетевой фетч был под try/except —
    позиция wishlist не той формы (например, список строк вместо dict'ов)
    роняла sorted()/item.get(...) необработанным AttributeError, который
    уходил из get_random_wishlist_game ДО session.commit() в
    awards_service.run_awards и мог откатить уже посчитанные выплаты
    номинаций за день. Теперь сортировка/форматирование тоже под
    try/except — деградация в None, как и сетевая ошибка."""
    _reset_cache(monkeypatch)
    _set_steam_settings(monkeypatch)
    monkeypatch.setattr(
        steam_service,
        "_fetch_wishlist_json",
        AsyncMock(return_value={"response": {"items": ["не словарь", 12345]}}),
    )

    result = await steam_service.get_random_wishlist_game(date(2026, 7, 19))

    assert result is None


@pytest.mark.asyncio
async def test_empty_wishlist_returns_none(monkeypatch):
    _reset_cache(monkeypatch)
    _set_steam_settings(monkeypatch)
    monkeypatch.setattr(
        steam_service,
        "_fetch_wishlist_json",
        AsyncMock(return_value={"response": {"items": []}}),
    )

    result = await steam_service.get_random_wishlist_game(date(2026, 7, 19))

    assert result is None


@pytest.mark.asyncio
async def test_missing_inline_name_resolves_via_appdetails(monkeypatch):
    """Реальный ответ IWishlistService несёт только appid (см. модульный
    docstring) — этот кейс резолвит имя через store/appdetails вместо
    fallback'а `Steam App #{appid}`."""
    _reset_cache(monkeypatch)
    _set_steam_settings(monkeypatch)
    monkeypatch.setattr(
        steam_service,
        "_fetch_wishlist_json",
        AsyncMock(return_value={"response": {"items": [{"appid": 42}]}}),
    )
    mock_appdetails = AsyncMock(
        return_value={"42": {"success": True, "data": {"name": "Elden Ring"}}}
    )
    monkeypatch.setattr(steam_service, "_fetch_appdetails_json", mock_appdetails)

    result = await steam_service.get_random_wishlist_game(date(2026, 7, 19))

    assert result == "Elden Ring"
    mock_appdetails.assert_awaited_once_with(42)


@pytest.mark.asyncio
async def test_appdetails_failure_falls_back_to_appid_label(monkeypatch):
    """D-11: appdetails недоступен -> `Steam App #{appid}`, НЕ None — сама
    игра уже найдена в wishlist, деградирует только имя, не весь результат."""
    _reset_cache(monkeypatch)
    _set_steam_settings(monkeypatch)
    monkeypatch.setattr(
        steam_service,
        "_fetch_wishlist_json",
        AsyncMock(return_value={"response": {"items": [{"appid": 42}]}}),
    )
    monkeypatch.setattr(
        steam_service, "_fetch_appdetails_json", AsyncMock(side_effect=Exception("boom"))
    )

    result = await steam_service.get_random_wishlist_game(date(2026, 7, 19))

    assert result == "Steam App #42"


@pytest.mark.asyncio
async def test_resolved_name_is_cached_across_calls(monkeypatch):
    """Повторный вызов в тот же день (тот же выбранный appid) не должен
    снова бить по appdetails — module-level `_name_cache`."""
    _reset_cache(monkeypatch)
    _set_steam_settings(monkeypatch)
    monkeypatch.setattr(
        steam_service,
        "_fetch_wishlist_json",
        AsyncMock(return_value={"response": {"items": [{"appid": 42}]}}),
    )
    mock_appdetails = AsyncMock(
        return_value={"42": {"success": True, "data": {"name": "Elden Ring"}}}
    )
    monkeypatch.setattr(steam_service, "_fetch_appdetails_json", mock_appdetails)

    day = date(2026, 7, 19)
    first = await steam_service.get_random_wishlist_game(day)
    second = await steam_service.get_random_wishlist_game(day)

    assert first == second == "Elden Ring"
    mock_appdetails.assert_awaited_once()
