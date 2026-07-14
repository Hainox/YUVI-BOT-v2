"""HTTP-клиент чтения внешних рынков Polymarket/Manifold (BET-02, половина парсинга).

Модуль НЕ импортирует ORM/модели и не пишет в БД — конвенция
`nlp_client.py` ("этот модуль НЕ импортирует ORM/модели"). Money-
инварианты и запись рынков остаются в `markets_service` (план 03-06):
этот файл только фетчит и нормализует внешний рынок в
`{question, options, closed, winning_label, external_id}`.

Используем aiohttp (уже транзитивная зависимость aiogram, уже используется в
nlp_client.py) — новый HTTP-пакет не добавляем.

SSRF (Pitfall 5, T-03-09): любой URL, кроме polymarket.com/manifold.markets,
отклоняется ДО какого-либо HTTP-запроса. Хост проверяется через
`urllib.parse.urlparse(...).hostname` (точное сравнение/allowlist), а НЕ
через `"polymarket.com" in url` или `re.search(...)` по всей строке —
подстрочная проверка обходится URL вида
`http://polymarket.com.evil.example/market/x` (хост на самом деле
`polymarket.com.evil.example`, но подстрока "polymarket.com" в нём есть).
Наружу всегда уходит только hardcoded `_GAMMA_BASE`/`_MANIFOLD_BASE` +
извлечённый regex-slug — сырой пользовательский URL никогда не передаётся в
aiohttp.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from urllib.parse import urlparse

import aiohttp

from bot.config import settings

logger = logging.getLogger(__name__)


class ExternalMarketError(Exception):
    """Базовое исключение модуля."""


class UnsupportedMarketUrl(ExternalMarketError):
    """URL не распознан, запрещён (SSRF-фильтр) или указывает на неподдерживаемый тип рынка."""


class MarketFetchError(ExternalMarketError):
    """Рынок не найден или сетевой сбой после исчерпания ретраев."""


_GAMMA_BASE = "https://gamma-api.polymarket.com"
_MANIFOLD_BASE = "https://api.manifold.markets/v0"

_POLYMARKET_HOSTS = {"polymarket.com", "www.polymarket.com"}
_MANIFOLD_HOSTS = {"manifold.markets", "www.manifold.markets"}

_POLYMARKET_MARKET_RE = re.compile(r"polymarket\.com/market/([a-z0-9-]+)", re.I)
_POLYMARKET_EVENT_RE = re.compile(r"polymarket\.com/event/([a-z0-9-]+)", re.I)
_MANIFOLD_RE = re.compile(r"manifold\.markets/[^/\s]+/([a-z0-9-]+)", re.I)


def _extract_host(url: str) -> str:
    """Единственный источник истины для SSRF-allowlist — реальный хост URL, не подстрока."""
    parsed = urlparse(url)
    return (parsed.hostname or "").lower()


async def _get_json(url: str, retries: int = 5, base_delay: float = 2.0) -> dict | list:
    """GET url с ретраем на ошибках соединения/таймаута (по образцу nlp_client._post_with_retry).

    url здесь — всегда hardcoded _GAMMA_BASE/_MANIFOLD_BASE + извлечённый
    slug, никогда сырой пользовательский URL (вызывающая сторона это
    гарантирует).
    """
    timeout = aiohttp.ClientTimeout(total=settings.ai_call_timeout_sec)

    last_exc: Exception | None = None
    for attempt in range(retries):
        try:
            async with aiohttp.ClientSession(timeout=timeout) as http_session:
                async with http_session.get(url) as response:
                    response.raise_for_status()
                    return await response.json()
        except (aiohttp.ClientConnectorError, asyncio.TimeoutError, aiohttp.ServerTimeoutError) as exc:
            last_exc = exc
            delay = base_delay * (2**attempt)
            logger.warning(
                "external_markets: попытка %s/%s к %s не удалась (%s), повтор через %.1fс",
                attempt + 1,
                retries,
                url,
                exc,
                delay,
            )
            if attempt < retries - 1:
                await asyncio.sleep(delay)

    raise MarketFetchError(f"Не удалось получить {url}: {last_exc}") from last_exc


async def _fetch_polymarket(url: str) -> dict:
    """Фетч и нормализация рынка Polymarket. outcomes/outcomePrices — JSON-строка внутри JSON
    (Pitfall 4) — требуется ДВОЙНОЙ json.loads."""
    if match := _POLYMARKET_MARKET_RE.search(url):
        slug = match.group(1)
        data = await _get_json(f"{_GAMMA_BASE}/markets?slug={slug}")
        if not data:
            raise MarketFetchError(f"Рынок Polymarket не найден: {slug}")
        market = data[0]
    elif match := _POLYMARKET_EVENT_RE.search(url):
        slug = match.group(1)
        event = await _get_json(f"{_GAMMA_BASE}/events/slug/{slug}")
        markets = event.get("markets", [])
        if len(markets) != 1:
            raise UnsupportedMarketUrl(
                "Это страница события с несколькими рынками — дайте ссылку на конкретный рынок"
            )
        market = markets[0]
    else:
        raise UnsupportedMarketUrl("Не распознан URL Polymarket")

    outcomes = json.loads(market["outcomes"])  # double-decode gotcha (Pitfall 4)
    prices = json.loads(market["outcomePrices"])  # double-decode gotcha (Pitfall 4)
    closed = market.get("closed", False)

    winning_label = None
    if closed:
        # Gamma API не отдаёт отдельное поле "winningOutcome" в /markets —
        # де-факто сигнал: цена победившего исхода оседает у 1.0 (эвристика,
        # Assumption A3 — RESEARCH.md Open Questions #1).
        for label, price in zip(outcomes, prices):
            if float(price) > 0.99:
                winning_label = label
                break

    return {
        "question": market["question"],
        "options": outcomes,
        "closed": closed,
        "winning_label": winning_label,
        "external_id": market.get("conditionId") or market["id"],
    }


async def _fetch_manifold(url: str) -> dict:
    """Фетч и нормализация рынка Manifold. Нативный JSON — без double-decode, но с
    отдельной семантикой resolution для BINARY/MULTIPLE_CHOICE."""
    match = _MANIFOLD_RE.search(url)
    if not match:
        raise UnsupportedMarketUrl("Не распознан URL Manifold")

    slug = match.group(1)
    market = await _get_json(f"{_MANIFOLD_BASE}/slug/{slug}")

    outcome_type = market.get("outcomeType")
    if outcome_type == "BINARY":
        options = ["Yes", "No"]
    elif outcome_type == "MULTIPLE_CHOICE":
        options = [answer["text"] for answer in market.get("answers", [])]
    else:
        raise UnsupportedMarketUrl(f"Тип рынка Manifold {outcome_type} не поддерживается")

    winning_label = None
    if market.get("isResolved"):
        resolution = market.get("resolution")
        if resolution in ("YES", "NO"):
            winning_label = "Yes" if resolution == "YES" else "No"
        elif resolution not in (None, "MKT", "CANCEL"):
            # multiple-choice: resolution хранит id/текст победившего варианта
            winning_label = next(
                (answer["text"] for answer in market.get("answers", []) if answer["id"] == resolution),
                resolution,
            )

    return {
        "question": market["question"],
        "options": options,
        "closed": market.get("isResolved", False),
        "winning_label": winning_label,
        "external_id": market["id"],
    }


async def fetch_external_market(url: str) -> dict:
    """Диспетчер по хосту URL: polymarket.com → _fetch_polymarket, manifold.markets →
    _fetch_manifold, иначе UnsupportedMarketUrl — ДО какого-либо HTTP-запроса (SSRF-фильтр,
    Pitfall 5, T-03-09)."""
    host = _extract_host(url)

    if host in _POLYMARKET_HOSTS:
        return await _fetch_polymarket(url)
    if host in _MANIFOLD_HOSTS:
        return await _fetch_manifold(url)

    raise UnsupportedMarketUrl(
        f"Поддерживаются только ссылки на polymarket.com и manifold.markets, получено: {host or url}"
    )
