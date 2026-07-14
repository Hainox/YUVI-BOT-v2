"""Unit-тесты bot.services.external_markets — без реальной сети и без Postgres.

Реальный HTTP полностью замокан: `external_markets._get_json` подменяется
через monkeypatch на AsyncMock/фикстуру-функцию, поэтому тесты детерминированы
и не зависят от живых Polymarket/Manifold API (по образцу
tests/test_nlp_classifier.py, где так же мокается nlp_client).

Ключевые кейсы:
- Polymarket: outcomes/outcomePrices — JSON-строка внутри JSON, нужен
  ДВОЙНОЙ json.loads (Pitfall 4, RESEARCH.md).
- Polymarket closed market: winning_label по эвристике price > 0.99
  (Assumption A3) — подтверждено ЖИВЫМ прогоном против реального Gamma API
  на реальном разрешённом рынке `will-trump-win-the-2020-us-presidential-
  election` в рамках чекпоинта 03-06 Task 3 (см. 03-06-SUMMARY.md):
  outcomePrices=["~0.0000000436", "~0.9999999563"] → "No" > 0.99, что
  совпадает с реальным исходом (Trump проиграл выборы 2020).
- Polymarket market-branch регрессия (03-06 Task 3, найдено при том же живом
  прогоне): `/markets?slug={slug}` (list-эндпоинт) применяет closed=false
  ДАЖЕ при заданном slug — для уже ЗАКРЫТОГО рынка живой ответ был `[]`,
  из-за чего auto_resolve_external НИКОГДА бы не находил разрешённые
  Polymarket-рынки. Фикс — выделенный `/markets/slug/{slug}` эндпоинт
  (возвращает единственный объект, не список; подтверждено live, что он
  корректно находит рынок независимо от closed/open, в отличие от `&closed=
  true`, который live-проверкой ломает поиск ОТКРЫТЫХ рынков по slug).
- Manifold: BINARY/MULTIPLE_CHOICE, resolution → winning_label.
- SSRF (Pitfall 5, T-03-09): URL с хостом, не входящим в allowlist
  {polymarket.com, manifold.markets}, отклоняется ДО какого-либо HTTP-запроса
  — доказываем, что _get_json ни разу не был вызван.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, Mock

import aiohttp
import pytest

from bot.services import external_markets


@pytest.mark.asyncio
async def test_polymarket_market_double_json_loads(monkeypatch):
    market = {
        "question": "Будет ли X?",
        "outcomes": '["Yes", "No"]',
        "outcomePrices": '["0.20", "0.80"]',
        "closed": False,
        "conditionId": "cond-123",
        "id": "999",
    }
    # /markets/slug/{slug} возвращает ЕДИНСТВЕННЫЙ объект, не список
    # (регрессия 03-06 Task 3 — см. модульный docstring).
    mock_get_json = AsyncMock(return_value=market)
    monkeypatch.setattr(external_markets, "_get_json", mock_get_json)

    result = await external_markets.fetch_external_market(
        "https://polymarket.com/market/will-x-happen"
    )

    assert result["question"] == "Будет ли X?"
    assert result["options"] == ["Yes", "No"]
    assert result["closed"] is False
    assert result["winning_label"] is None
    assert result["external_id"] == "cond-123"
    mock_get_json.assert_awaited_once()
    requested_url = mock_get_json.call_args.args[0]
    assert requested_url.startswith(external_markets._GAMMA_BASE)
    assert "will-x-happen" in requested_url


@pytest.mark.asyncio
async def test_polymarket_market_uses_dedicated_slug_endpoint_not_filtered_list(monkeypatch):
    """Регрессия 03-06 Task 3: `/markets?slug={slug}` (list-эндпоинт) на реальном
    Gamma API молча применяет closed=false даже с заданным slug и отдаёт `[]`
    для уже закрытого рынка — auto_resolve_external никогда не находил бы
    разрешённые Polymarket-рынки. Фикс — `/markets/slug/{slug}` (выделенный
    by-slug эндпоинт, отдаёт рынок независимо от open/closed — подтверждено
    live). Тест доказывает: (а) запрошенный URL — именно `/markets/slug/`, а
    НЕ `/markets?slug=`; (б) фикс работает и для закрытого, и для открытого
    рынка (не однобокая правка вида `&closed=true`, которая на реальном API
    ломает поиск открытых рынков — см. docstring модуля)."""
    closed_market = {
        "question": "Закрытый рынок",
        "outcomes": '["Yes", "No"]',
        "outcomePrices": '["0.995", "0.005"]',
        "closed": True,
        "conditionId": "cond-closed",
        "id": "1",
    }
    open_market = {
        "question": "Открытый рынок",
        "outcomes": '["Yes", "No"]',
        "outcomePrices": '["0.5", "0.5"]',
        "closed": False,
        "conditionId": "cond-open",
        "id": "2",
    }

    async def _fake_get_json(url: str):
        # Единственный by-slug эндпоинт — независимо от open/closed рынка.
        assert "/markets/slug/" in url
        assert "?slug=" not in url
        if "closed-market-slug" in url:
            return closed_market
        if "open-market-slug" in url:
            return open_market
        raise AssertionError(f"unexpected url: {url}")

    monkeypatch.setattr(external_markets, "_get_json", _fake_get_json)

    closed_result = await external_markets.fetch_external_market(
        "https://polymarket.com/market/closed-market-slug"
    )
    assert closed_result["closed"] is True
    assert closed_result["winning_label"] == "Yes"

    open_result = await external_markets.fetch_external_market(
        "https://polymarket.com/market/open-market-slug"
    )
    assert open_result["closed"] is False
    assert open_result["winning_label"] is None


@pytest.mark.asyncio
async def test_polymarket_market_not_found_404_raises_market_fetch_error(monkeypatch):
    """Регрессия 03-06 Task 3: выделенный by-slug эндпоинт на отсутствующий
    slug отвечает 404 (проверено live), а не `200 []` как list-эндпоинт.
    `_get_json` пробрасывает `aiohttp.ClientResponseError` через
    `raise_for_status()` — без явной обёртки (`_get_json_by_slug`) это ушло
    бы наверх непойманным вместо ожидаемого `MarketFetchError`."""
    request_info = Mock(real_url="https://gamma-api.polymarket.com/markets/slug/missing")
    not_found = aiohttp.ClientResponseError(
        request_info=request_info, history=(), status=404, message="Not Found"
    )
    monkeypatch.setattr(external_markets, "_get_json", AsyncMock(side_effect=not_found))

    with pytest.raises(external_markets.MarketFetchError):
        await external_markets.fetch_external_market(
            "https://polymarket.com/market/missing-slug"
        )


@pytest.mark.asyncio
async def test_polymarket_closed_winning_label_by_price_threshold(monkeypatch):
    market = {
        "question": "Уже решено?",
        "outcomes": '["Yes", "No"]',
        "outcomePrices": '["0.995", "0.005"]',
        "closed": True,
        "conditionId": "cond-456",
        "id": "1000",
    }
    # /markets/slug/{slug} возвращает единственный объект, не список.
    monkeypatch.setattr(external_markets, "_get_json", AsyncMock(return_value=market))

    result = await external_markets.fetch_external_market(
        "https://polymarket.com/market/already-resolved"
    )

    assert result["closed"] is True
    assert result["winning_label"] == "Yes"


@pytest.mark.asyncio
async def test_polymarket_event_multiple_markets_rejected(monkeypatch):
    event = {
        "markets": [
            {"question": "A", "outcomes": "[]", "outcomePrices": "[]", "id": "1"},
            {"question": "B", "outcomes": "[]", "outcomePrices": "[]", "id": "2"},
        ]
    }
    monkeypatch.setattr(external_markets, "_get_json", AsyncMock(return_value=event))

    with pytest.raises(external_markets.UnsupportedMarketUrl):
        await external_markets.fetch_external_market(
            "https://polymarket.com/event/some-event-with-many-markets"
        )


@pytest.mark.asyncio
async def test_manifold_binary(monkeypatch):
    market = {
        "id": "m1",
        "question": "Будет ли Y?",
        "outcomeType": "BINARY",
        "isResolved": True,
        "resolution": "YES",
    }
    mock_get_json = AsyncMock(return_value=market)
    monkeypatch.setattr(external_markets, "_get_json", mock_get_json)

    result = await external_markets.fetch_external_market(
        "https://manifold.markets/someuser/will-y-happen"
    )

    assert result["question"] == "Будет ли Y?"
    assert result["options"] == ["Yes", "No"]
    assert result["closed"] is True
    assert result["winning_label"] == "Yes"
    assert result["external_id"] == "m1"
    requested_url = mock_get_json.call_args.args[0]
    assert requested_url.startswith(external_markets._MANIFOLD_BASE)


@pytest.mark.asyncio
async def test_manifold_multiple_choice(monkeypatch):
    market = {
        "id": "m2",
        "question": "Какой из вариантов?",
        "outcomeType": "MULTIPLE_CHOICE",
        "isResolved": False,
        "answers": [
            {"id": "a1", "text": "Альфа"},
            {"id": "a2", "text": "Бета"},
        ],
    }
    monkeypatch.setattr(external_markets, "_get_json", AsyncMock(return_value=market))

    result = await external_markets.fetch_external_market(
        "https://manifold.markets/someuser/which-option"
    )

    assert result["options"] == ["Альфа", "Бета"]
    assert result["closed"] is False
    assert result["winning_label"] is None


@pytest.mark.asyncio
async def test_manifold_multiple_choice_resolved_winning_label(monkeypatch):
    market = {
        "id": "m3",
        "question": "Какой из вариантов победил?",
        "outcomeType": "MULTIPLE_CHOICE",
        "isResolved": True,
        "resolution": "a2",
        "answers": [
            {"id": "a1", "text": "Альфа"},
            {"id": "a2", "text": "Бета"},
        ],
    }
    monkeypatch.setattr(external_markets, "_get_json", AsyncMock(return_value=market))

    result = await external_markets.fetch_external_market(
        "https://manifold.markets/someuser/which-option-resolved"
    )

    assert result["winning_label"] == "Бета"


@pytest.mark.asyncio
async def test_manifold_unsupported_type_rejected(monkeypatch):
    market = {
        "id": "m4",
        "question": "Числовой рынок",
        "outcomeType": "NUMERIC",
    }
    monkeypatch.setattr(external_markets, "_get_json", AsyncMock(return_value=market))

    with pytest.raises(external_markets.UnsupportedMarketUrl):
        await external_markets.fetch_external_market(
            "https://manifold.markets/someuser/numeric-market"
        )


@pytest.mark.asyncio
async def test_ssrf_non_provider_url_rejected(monkeypatch):
    mock_get_json = AsyncMock()
    monkeypatch.setattr(external_markets, "_get_json", mock_get_json)

    for dangerous_url in (
        "http://169.254.169.254/latest/meta-data/",
        "http://localhost:5432/",
        "https://polymarket.com.evil.example/market/foo",
        "http://evil.example/manifold.markets/user/slug",
    ):
        with pytest.raises(external_markets.UnsupportedMarketUrl):
            await external_markets.fetch_external_market(dangerous_url)

    mock_get_json.assert_not_awaited()


@pytest.mark.asyncio
async def test_dispatch_routes_by_host(monkeypatch):
    polymarket_market = {
        "question": "Polymarket-вопрос",
        "outcomes": '["Yes", "No"]',
        "outcomePrices": '["0.5", "0.5"]',
        "closed": False,
        "id": "poly-1",
    }
    manifold_market = {
        "id": "mani-1",
        "question": "Manifold-вопрос",
        "outcomeType": "BINARY",
        "isResolved": False,
    }

    async def _fake_get_json(url: str):
        if url.startswith(external_markets._GAMMA_BASE):
            # /markets/slug/{slug} возвращает единственный объект, не список.
            return polymarket_market
        if url.startswith(external_markets._MANIFOLD_BASE):
            return manifold_market
        raise AssertionError(f"unexpected url: {url}")

    monkeypatch.setattr(external_markets, "_get_json", _fake_get_json)

    poly_result = await external_markets.fetch_external_market(
        "https://polymarket.com/market/poly-question"
    )
    assert poly_result["question"] == "Polymarket-вопрос"

    manifold_result = await external_markets.fetch_external_market(
        "https://manifold.markets/someuser/manifold-question"
    )
    assert manifold_result["question"] == "Manifold-вопрос"
