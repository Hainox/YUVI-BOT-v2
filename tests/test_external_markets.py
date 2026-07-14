"""Unit-тесты bot.services.external_markets — без реальной сети и без Postgres.

Реальный HTTP полностью замокан: `external_markets._get_json` подменяется
через monkeypatch на AsyncMock/фикстуру-функцию, поэтому тесты детерминированы
и не зависят от живых Polymarket/Manifold API (по образцу
tests/test_nlp_classifier.py, где так же мокается nlp_client).

Ключевые кейсы:
- Polymarket: outcomes/outcomePrices — JSON-строка внутри JSON, нужен
  ДВОЙНОЙ json.loads (Pitfall 4, RESEARCH.md).
- Polymarket closed market: winning_label по эвристике price > 0.99
  (Assumption A3).
- Manifold: BINARY/MULTIPLE_CHOICE, resolution → winning_label.
- SSRF (Pitfall 5, T-03-09): URL с хостом, не входящим в allowlist
  {polymarket.com, manifold.markets}, отклоняется ДО какого-либо HTTP-запроса
  — доказываем, что _get_json ни разу не был вызван.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

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
    mock_get_json = AsyncMock(return_value=[market])
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
async def test_polymarket_closed_winning_label_by_price_threshold(monkeypatch):
    market = {
        "question": "Уже решено?",
        "outcomes": '["Yes", "No"]',
        "outcomePrices": '["0.995", "0.005"]',
        "closed": True,
        "conditionId": "cond-456",
        "id": "1000",
    }
    monkeypatch.setattr(external_markets, "_get_json", AsyncMock(return_value=[market]))

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
            return [polymarket_market]
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
