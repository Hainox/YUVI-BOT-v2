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

import time
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
    monkeypatch.setattr(steam_service, "_daily_pick_cache", None)


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


@pytest.mark.asyncio
async def test_item_without_appid_or_name_falls_back_to_placeholder(monkeypatch):
    """Ветка 'chosen без appid и без name' (повреждённый/пустой элемент
    wishlist среди в остальном валидных позиций, например баг сериализации
    на стороне Steam) — отличается от test_malformed_item_shape_graceful_none,
    где элементы вообще не dict и падают на sorted()/AttributeError ДО этой
    ветки. Здесь sorted_items валиден, RNG выбирает конкретно пустой dict,
    и код должен вернуть литерал 'Steam App #?', а не 'Steam App #None' и не
    None."""
    _reset_cache(monkeypatch)
    _set_steam_settings(monkeypatch)
    monkeypatch.setattr(
        steam_service,
        "_fetch_wishlist_json",
        AsyncMock(
            return_value={
                "response": {
                    "items": [
                        {"appid": 10, "name": "Игра A"},
                        {"appid": 20, "name": "Игра B"},
                        {},
                    ]
                }
            }
        ),
    )

    # Дата подобрана явно так, чтобы random.Random(day.toordinal()).choice(...)
    # после сортировки по appid (missing -> 0, значит {} идёт первым)
    # детерминированно попадал на повреждённый элемент {} — проверено
    # вычислением, не полагаемся на "авось повезёт".
    result = await steam_service.get_random_wishlist_game(date(2026, 1, 1))

    assert result == "Steam App #?"


@pytest.mark.asyncio
async def test_same_day_pick_stable_despite_wishlist_ttl_refresh(monkeypatch):
    """Регрессионный тест на нарушение гарантии из докстринга модуля
    ('day-seeded детерминизм — единственный источник стабильности повторного
    /awards в тот же день'): TTL-кэш сырого wishlist (6ч) может истечь в
    середине MSK-дня, а состав живого Steam-wishlist за это время —
    измениться. Проверено вычислением: для day=2026-07-20 V1 даёт 'B', а V2
    (с изменённым составом) дало бы 'NEW', если бы выбор пересчитывался
    заново после рефетча. Фикс — module-level `_daily_pick_cache`, хранящий
    сам итог выбора за день (а не только сырой список), поэтому второй вызов
    в тот же день не должен даже обращаться к HTTP повторно."""
    _reset_cache(monkeypatch)
    _set_steam_settings(monkeypatch)

    v1 = {
        "response": {
            "items": [
                {"appid": 10, "name": "A"},
                {"appid": 20, "name": "B"},
                {"appid": 30, "name": "C"},
            ]
        }
    }
    v2 = {
        "response": {
            "items": [
                {"appid": 10, "name": "A"},
                {"appid": 15, "name": "NEW"},
                {"appid": 20, "name": "B"},
            ]
        }
    }
    mock_fetch = AsyncMock(return_value=v1)
    monkeypatch.setattr(steam_service, "_fetch_wishlist_json", mock_fetch)

    day = date(2026, 7, 20)
    first = await steam_service.get_random_wishlist_game(day)
    assert first == "B"

    # Симулируем истечение 6ч TTL-кэша сырого wishlist позже в течение ТОГО
    # ЖЕ MSK-дня, а также изменение состава живого wishlist за это время.
    monkeypatch.setattr(
        steam_service, "_cache_fetched_at", time.monotonic() - steam_service._CACHE_TTL_SEC - 10
    )
    mock_fetch.return_value = v2

    second = await steam_service.get_random_wishlist_game(day)

    assert second == first == "B"
    mock_fetch.assert_awaited_once()  # второй вызов не должен был бить по HTTP вообще


@pytest.mark.asyncio
async def test_appdetails_explicit_success_false_falls_back_to_appid_label(monkeypatch):
    """Отдельная code-path от test_appdetails_failure_falls_back_to_appid_label
    (там весь _fetch_appdetails_json бросает исключение целиком, ловится
    except-блоком _resolve_app_name). Здесь HTTP проходит успешно, JSON
    валиден, но Steam явно отдаёт success: False (реальный формат ответа
    appdetails для delisted/возрастных/регион-заблокированных приложений) —
    БЕЗ исключения. Тернарник `... if entry.get('success') else None` должен
    сам по себе деградировать в fallback, минуя except-блок."""
    _reset_cache(monkeypatch)
    _set_steam_settings(monkeypatch)
    monkeypatch.setattr(
        steam_service,
        "_fetch_wishlist_json",
        AsyncMock(return_value={"response": {"items": [{"appid": 42}]}}),
    )
    mock_appdetails = AsyncMock(return_value={"42": {"success": False}})
    monkeypatch.setattr(steam_service, "_fetch_appdetails_json", mock_appdetails)

    result = await steam_service.get_random_wishlist_game(date(2026, 7, 19))

    assert result == "Steam App #42"
    mock_appdetails.assert_awaited_once_with(42)


@pytest.mark.asyncio
async def test_wishlist_ttl_cache_boundary_is_exclusive(monkeypatch):
    """Строгое '<' в _get_wishlist_items (не '<='): ровно на границе
    (now - fetched_at) == _CACHE_TTL_SEC кэш должен считаться уже протухшим,
    а не ещё валидным. time.monotonic() зафиксирован моком на constant, чтобы
    сравнение было ТОЧНО равно TTL, а не приблизительно (реальные часы не
    гарантируют такую точность между setup и вызовом)."""
    _reset_cache(monkeypatch)
    _set_steam_settings(monkeypatch)

    fixed_now = 1_000_000.0
    monkeypatch.setattr(steam_service.time, "monotonic", lambda: fixed_now)
    monkeypatch.setattr(steam_service, "_cache_items", [{"appid": 99, "name": "Stale"}])
    monkeypatch.setattr(
        steam_service, "_cache_fetched_at", fixed_now - steam_service._CACHE_TTL_SEC
    )

    mock_fetch = AsyncMock(return_value={"response": {"items": [{"appid": 1, "name": "Fresh"}]}})
    monkeypatch.setattr(steam_service, "_fetch_wishlist_json", mock_fetch)

    result = await steam_service.get_random_wishlist_game(date(2026, 7, 19))

    mock_fetch.assert_awaited_once()  # кэш признан протухшим на границе -> рефетч
    assert result == "Fresh"
