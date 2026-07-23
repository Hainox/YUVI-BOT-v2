"""Клиент к self-hosted cobalt (MEDIA-01/02, D-05/D-06/D-07/D-08) +
SSRF-whitelist на входе.

Сервис НЕ пишет `user_balance`/`chat_bank`/`economy_tx` и не коммитит
транзакцию — списание (`economy_service.debit_to_bank`) и `session.commit()`
делает хендлер (`bot/handlers/media_dl.py`), СТРОГО после успешной загрузки
(D-07, charge-only-on-success, 06-RESEARCH.md Pattern 2).

Используем aiohttp (уже транзитивная зависимость aiogram, тот же клиент, что
`bot/services/nlp_client.py`) — новый HTTP-пакет не добавляем.

SSRF-whitelist (T-06-03): `URL_RE` распознаёт ТОЛЬКО TikTok / Instagram Reels
/ YouTube Shorts — произвольный URL (включая localhost/внутренние IP и даже
обычные youtube.com/instagram.com ссылки, не reel/shorts) вообще не доходит
до POST к cobalt.

Дневной лимит на пользователя (`settings.mediadl_daily_limit`, по просьбе
пользователя при включении скачивания в группах обратно): `count_today` —
чистое чтение,
СЧИТАЕТ уже существующие строки `economy_tx` с `kind="mediadl_charge"`
(каждая успешная доставка пишет ровно одну такую строку на игрока,
`economy_service.debit_to_bank`), отдельная таблица-счётчик не заводится.
Граница дня — Europe/Moscow (та же конвенция, что `daily_pick_service.
_today_msk()`/`stats_service.py`/`mood_service.py` — каждый модуль держит
собственный self-contained `_today_msk()`-аналог, не импортирует чужой).
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from datetime import time
from zoneinfo import ZoneInfo

import aiohttp
from aiogram.types import InputMediaAnimation
from aiogram.types import InputMediaPhoto
from aiogram.types import InputMediaVideo
from sqlalchemy import func
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import settings
from common.models.economy_tx import EconomyTx

logger = logging.getLogger(__name__)

MSK = ZoneInfo("Europe/Moscow")
UTC = ZoneInfo("UTC")

# SSRF-whitelist (T-06-03) — только три площадки из D-01/MEDIA-01. Обычные
# youtube.com/watch и instagram.com/p (не reel) НАМЕРЕННО не матчатся —
# catch-all существует только для TikTok/Reels/Shorts (D-08), не для любого
# youtube/instagram URL.
URL_RE = re.compile(
    r"https?://"
    r"(?:"
    r"(?:www\.|vm\.|vt\.)?tiktok\.com/\S+"
    r"|(?:www\.)?instagram\.com/reel/\S+"
    r"|(?:www\.|m\.)?youtube\.com/shorts/\S+"
    r"|youtu\.be/\S+"
    r")",
    re.IGNORECASE,
)

# Cobalt picker item `type` -> aiogram InputMedia* (Pitfall 4). Неизвестный
# тип падает на InputMediaVideo — cobalt всегда присылает валидный видео-
# контейнер для не-photo/gif элементов (06-RESEARCH.md A3).
PICKER_TYPE_MAP: dict[str, type] = {
    "photo": InputMediaPhoto,
    "video": InputMediaVideo,
    "gif": InputMediaAnimation,
}

# Лимит sendMediaGroup Telegram Bot API (D-06).
MEDIA_GROUP_LIMIT = 10

_RESOLVE_TIMEOUT_SEC = 30
_DOWNLOAD_TIMEOUT_SEC = 120
_DOWNLOAD_CHUNK_SIZE = 65536


def extract_url(text: str) -> str | None:
    """Первая распознанная ссылка (TikTok/Reels/Shorts) в тексте или None."""
    if not text:
        return None
    match = URL_RE.search(text)
    return match.group(0) if match else None


def _today_msk_start_utc() -> datetime:
    """Начало текущих суток по Europe/Moscow, в наивном UTC — сравнимо
    напрямую с `EconomyTx.created_at` (`DateTime` без таймзоны, пишется через
    `func.now()`/`datetime.utcnow()` по всей кодовой базе, тот же контракт,
    что и остальные `_today_msk()`-аналоги)."""
    today_msk = datetime.now(MSK).date()
    start_msk = datetime.combine(today_msk, time.min, tzinfo=MSK)
    return start_msk.astimezone(UTC).replace(tzinfo=None)


async def count_today(session: AsyncSession, chat_id: int, user_id: int) -> int:
    """Сколько скачиваний (`kind="mediadl_charge"`, только строки игрока —
    банковское зеркало имеет `user_id IS NULL` и в счёт не попадает) уже
    списано с этого пользователя в этом чате с начала текущих MSK-суток.
    Чистое чтение — не коммитит, ничего не пишет."""
    count = (
        await session.execute(
            select(func.count()).where(
                EconomyTx.chat_id == chat_id,
                EconomyTx.user_id == user_id,
                EconomyTx.kind == "mediadl_charge",
                EconomyTx.created_at >= _today_msk_start_utc(),
            )
        )
    ).scalar_one()
    return count


async def resolve(url: str) -> dict:
    """POST settings.cobalt_api_url {url, videoQuality} — cobalt API v11 контракт.

    `raise_for_status()` (CR-02 06-REVIEW.md) — HTTP-уровневая ошибка cobalt
    (5xx/4xx от самого сервиса, отдельно от application-level `status:
    "error"` внутри валидного 200-ответа) поднимает `aiohttp.ClientResponseError`,
    не даёт вызывающему хендлеру трактовать битый/пустой ответ как валидный
    JSON. Source: 06-RESEARCH.md Pattern 2 (та же форма aiohttp, что
    nlp_client.py)."""
    timeout = aiohttp.ClientTimeout(total=_RESOLVE_TIMEOUT_SEC)
    async with aiohttp.ClientSession(timeout=timeout) as http_session:
        async with http_session.post(
            settings.cobalt_api_url,
            json={"url": url, "videoQuality": "720"},
            headers={"Accept": "application/json", "Content-Type": "application/json"},
        ) as response:
            response.raise_for_status()
            return await response.json()


async def download(item_url: str, max_bytes: int) -> bytes | None:
    """Стримит `item_url` чанками, обрывает РАННО при превышении `max_bytes`.

    Возвращает None при превышении лимита — не копит полный файл в память,
    если он заведомо больше max_bytes (T-06-11, DoS-защита). `raise_for_status()`
    (CR-02 06-REVIEW.md) — сломанный tunnel/CDN-ответ (4xx/5xx) поднимает
    `aiohttp.ClientResponseError` ДО того, как тело ответа начнёт стримиться
    и трактоваться как валидные байты файла — без этой проверки ошибка
    сервера тихо принималась бы за успешную загрузку и оплачивалась (D-07)."""
    timeout = aiohttp.ClientTimeout(total=_DOWNLOAD_TIMEOUT_SEC)
    chunks: list[bytes] = []
    total = 0
    async with aiohttp.ClientSession(timeout=timeout) as http_session:
        async with http_session.get(item_url) as response:
            response.raise_for_status()
            async for chunk in response.content.iter_chunked(_DOWNLOAD_CHUNK_SIZE):
                total += len(chunk)
                if total > max_bytes:
                    return None
                chunks.append(chunk)
    return b"".join(chunks)


def map_error(result: dict) -> str:
    """cobalt status/error -> русская строка (D-07 отказ-путь)."""
    status = result.get("status")
    if status == "local-processing":
        return "Эту ссылку нельзя скачать напрямую — cobalt требует обработки на устройстве."
    error = result.get("error") or {}
    code = error.get("code", "unknown")
    return f"Не удалось скачать медиа (ошибка cobalt: {code}). Попробуйте другую ссылку."


def picker_media_class(item_type: str | None) -> type:
    """Cobalt picker item `type` -> aiogram InputMedia* класс (Pitfall 4)."""
    return PICKER_TYPE_MAP.get(item_type or "", InputMediaVideo)


def cap_picker(picker: list[dict]) -> list[dict]:
    """Обрезает picker-список до лимита media group (D-06)."""
    return picker[:MEDIA_GROUP_LIMIT]
