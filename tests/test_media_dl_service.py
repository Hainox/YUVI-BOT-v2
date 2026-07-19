"""Wave 0 тесты MEDIA-01/02 (`bot/services/media_dl_service.py`,
`bot/handlers/media_dl.py`) — D-05/D-06/D-07/D-08.

RED (Task 1): `bot.services.media_dl_service` ещё не существует —
`from bot.services import media_dl_service` падает ImportError. Реализация —
Task 2 (GREEN), по образцам read_first / 06-RESEARCH.md Pattern 2.

cobalt HTTP мокается monkeypatch'ем на `aiohttp.ClientSession` (форма
`bot/services/nlp_client.py`: async context-manager `.post()`/`.get()` ->
response с `.json()` и `.content.iter_chunked()`), тот же принцип, что
`tests/conftest.py::bot` (AsyncMock вместо реального Telegram Bot).
charge-only-on-success тестируется на уровне ПОЛНОГО хендлера
(`bot/handlers/media_dl.py::on_media_url`) против живого Postgres (фикстура
`session` из tests/conftest.py, транзакция-на-тест) — тот же паттерн, что
`tests/test_economy_handlers.py::_fake_message` (SimpleNamespace вместо
реального aiogram Message).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiohttp
import pytest
from sqlalchemy import func
from sqlalchemy import select

from bot.config import settings
from bot.services import economy_service
from common.models.economy_tx import EconomyTx
from common.models.user import User
from common.models.user_balance import UserBalance

CHAT_A = -900701
CHAT_B = -900702
CHAT_C = -900703


async def _ensure_user(session, user_id: int, first_name: str = "Тест") -> None:
    session.add(User(id=user_id, first_name=first_name))
    await session.flush()


async def _get_user_balance(session, chat_id: int, user_id: int) -> int:
    result = await session.execute(
        select(UserBalance.balance).where(
            UserBalance.chat_id == chat_id, UserBalance.user_id == user_id
        )
    )
    return result.scalar_one()


async def _count_tx(session, chat_id: int, kind: str) -> int:
    result = await session.execute(
        select(func.count()).select_from(EconomyTx).where(
            EconomyTx.chat_id == chat_id, EconomyTx.kind == kind
        )
    )
    return result.scalar_one()


def _fake_message(
    chat_id: int,
    user_id: int,
    first_name: str,
    text: str,
    *,
    message_id: int = 1,
):
    """Минимальный aiogram-подобный Message — форма
    tests/test_economy_handlers.py::_fake_message, только атрибуты, которые
    реально читает on_media_url."""
    return SimpleNamespace(
        chat=SimpleNamespace(id=chat_id),
        from_user=SimpleNamespace(id=user_id, first_name=first_name),
        message_id=message_id,
        text=text,
        reply=AsyncMock(),
    )


# --- Моки aiohttp (для download-тестов, форма nlp_client.py) ----------------


class _FakeContentStream:
    """Имитирует `response.content.iter_chunked` — асинхронный генератор чанков."""

    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks

    def iter_chunked(self, size: int):
        async def _gen():
            for chunk in self._chunks:
                yield chunk

        return _gen()


class _FakeResponse:
    def __init__(self, json_data: dict | None = None, chunks: list[bytes] | None = None) -> None:
        self._json_data = json_data
        self.content = _FakeContentStream(chunks or [])

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def json(self):
        return self._json_data

    def raise_for_status(self) -> None:
        return None


class _FakeClientSession:
    def __init__(self, post_response=None, get_response=None) -> None:
        self._post_response = post_response
        self._get_response = get_response

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def post(self, *args, **kwargs):
        return self._post_response

    def get(self, *args, **kwargs):
        return self._get_response


# --- extract_url whitelist (T-06-03, SSRF) ------------------------------


def test_extract_url_whitelist():
    from bot.services import media_dl_service

    matching = [
        "смотри https://www.tiktok.com/@user/video/123456",
        "https://vm.tiktok.com/ZMabcdef/",
        "https://www.instagram.com/reel/Cabc123/",
        "https://www.youtube.com/shorts/abc123",
        "https://youtu.be/abc123",
    ]
    for text in matching:
        assert media_dl_service.extract_url(text) is not None, text

    rejecting = [
        "https://example.com",
        "http://localhost/foo",
        "http://127.0.0.1:9000/",
        "http://192.168.1.1/bar",
        "https://www.youtube.com/watch?v=abc123",
        "https://www.instagram.com/p/Cabc123/",
        "просто текст без ссылки",
        "",
    ]
    for text in rejecting:
        assert media_dl_service.extract_url(text) is None, text


# --- download: ранний обрыв при превышении size cap ------------------------


@pytest.mark.asyncio
async def test_download_aborts_over_size_cap(monkeypatch):
    from bot.services import media_dl_service

    big_chunk = b"x" * 1024
    fake_response = _FakeResponse(chunks=[big_chunk] * 10)  # 10КБ суммарно
    fake_session = _FakeClientSession(get_response=fake_response)
    monkeypatch.setattr(aiohttp, "ClientSession", lambda *a, **kw: fake_session)

    result = await media_dl_service.download("http://cobalt:9000/tunnel/abc", max_bytes=2048)
    assert result is None


@pytest.mark.asyncio
async def test_download_returns_bytes_within_cap(monkeypatch):
    from bot.services import media_dl_service

    chunk = b"x" * 100
    fake_response = _FakeResponse(chunks=[chunk] * 5)  # 500 байт суммарно
    fake_session = _FakeClientSession(get_response=fake_response)
    monkeypatch.setattr(aiohttp, "ClientSession", lambda *a, **kw: fake_session)

    result = await media_dl_service.download("http://cobalt:9000/tunnel/abc", max_bytes=2048)
    assert result == chunk * 5


# --- picker type mapping (Pitfall 4, D-06) ----------------------------------


def test_picker_type_mapping():
    from aiogram.types import InputMediaAnimation
    from aiogram.types import InputMediaPhoto
    from aiogram.types import InputMediaVideo

    from bot.services import media_dl_service

    assert media_dl_service.picker_media_class("photo") is InputMediaPhoto
    assert media_dl_service.picker_media_class("video") is InputMediaVideo
    assert media_dl_service.picker_media_class("gif") is InputMediaAnimation
    # Неизвестный/отсутствующий тип падает на InputMediaVideo (безопасный фолбэк).
    assert media_dl_service.picker_media_class("unknown") is InputMediaVideo
    assert media_dl_service.picker_media_class(None) is InputMediaVideo

    picker = [{"type": "photo", "url": f"http://x/{i}"} for i in range(15)]
    capped = media_dl_service.cap_picker(picker)
    assert len(capped) == 10


# --- map_error -> русские строки --------------------------------------------


def test_map_error_russian():
    from bot.services import media_dl_service

    error_text = media_dl_service.map_error(
        {"status": "error", "error": {"code": "error.api.link.invalid"}}
    )
    assert error_text and "error.api.link.invalid" not in error_text.lower() or "error.api" in error_text
    # Русский текст должен присутствовать (кириллица), а не сырой код ошибки.
    assert any("а" <= ch <= "я" or "А" <= ch <= "Я" for ch in error_text)

    local_text = media_dl_service.map_error({"status": "local-processing"})
    assert any("а" <= ch <= "я" or "А" <= ch <= "Я" for ch in local_text)
    assert local_text != error_text


# --- charge-only-on-success (D-07, полный хендлер) --------------------------


@pytest.mark.asyncio
async def test_charge_only_on_success(session, monkeypatch):
    import bot.handlers.media_dl as media_dl
    from bot.services import media_dl_service

    # --- 1. cobalt status="error" -> НЕ списываем -------------------------
    chat_id, user_id = CHAT_A, 900701
    await _ensure_user(session, user_id)
    await economy_service.get_balance(session, chat_id, user_id)

    async def fake_resolve_error(url: str) -> dict:
        return {"status": "error", "error": {"code": "error.api.link.invalid"}}

    monkeypatch.setattr(media_dl_service, "resolve", fake_resolve_error)

    message = _fake_message(chat_id, user_id, "Тест", "https://www.tiktok.com/@u/video/1", message_id=101)
    bot_mock = AsyncMock()
    await media_dl.on_media_url(message, session, bot_mock)

    assert await _count_tx(session, chat_id, "mediadl_charge") == 0
    assert await _get_user_balance(session, chat_id, user_id) == settings.economy_start_bonus
    bot_mock.send_video.assert_not_awaited()
    bot_mock.send_media_group.assert_not_awaited()
    message.reply.assert_awaited_once()

    # --- 2. cobalt status="local-processing" -> НЕ списываем ----------------
    chat_id2, user_id2 = CHAT_B, 900702
    await _ensure_user(session, user_id2)
    await economy_service.get_balance(session, chat_id2, user_id2)

    async def fake_resolve_local(url: str) -> dict:
        return {"status": "local-processing"}

    monkeypatch.setattr(media_dl_service, "resolve", fake_resolve_local)

    message2 = _fake_message(chat_id2, user_id2, "Тест", "https://www.tiktok.com/@u/video/2", message_id=102)
    bot_mock2 = AsyncMock()
    await media_dl.on_media_url(message2, session, bot_mock2)

    assert await _count_tx(session, chat_id2, "mediadl_charge") == 0
    assert await _get_user_balance(session, chat_id2, user_id2) == settings.economy_start_bonus
    bot_mock2.send_video.assert_not_awaited()

    # --- 3. успех: tunnel + загрузка в пределах лимита -> ровно один debit --
    chat_id3, user_id3 = CHAT_C, 900703
    await _ensure_user(session, user_id3)
    await economy_service.get_balance(session, chat_id3, user_id3)

    async def fake_resolve_tunnel(url: str) -> dict:
        return {"status": "tunnel", "url": "http://cobalt:9000/tunnel/abc", "filename": "video.mp4"}

    async def fake_download(item_url: str, max_bytes: int) -> bytes:
        return b"fake-video-bytes"

    monkeypatch.setattr(media_dl_service, "resolve", fake_resolve_tunnel)
    monkeypatch.setattr(media_dl_service, "download", fake_download)

    ref_id = "mediadl:103"
    message3 = _fake_message(chat_id3, user_id3, "Тест", "https://www.tiktok.com/@u/video/3", message_id=103)
    bot_mock3 = AsyncMock()
    await media_dl.on_media_url(message3, session, bot_mock3)

    assert await _count_tx(session, chat_id3, "mediadl_charge") == 1
    assert (
        await _get_user_balance(session, chat_id3, user_id3)
        == settings.economy_start_bonus - settings.mediadl_cost
    )
    bot_mock3.send_video.assert_awaited_once()

    # --- 4. повтор того же message_id -> идемпотентно, НЕ задваивает --------
    message3_retry = _fake_message(
        chat_id3, user_id3, "Тест", "https://www.tiktok.com/@u/video/3", message_id=103
    )
    bot_mock3_retry = AsyncMock()
    await media_dl.on_media_url(message3_retry, session, bot_mock3_retry)

    assert await _count_tx(session, chat_id3, "mediadl_charge") == 1
    assert (
        await _get_user_balance(session, chat_id3, user_id3)
        == settings.economy_start_bonus - settings.mediadl_cost
    )
    bot_mock3_retry.send_video.assert_not_awaited()
    assert ref_id  # ref_id формируется как f"mediadl:{message.message_id}" внутри хендлера
