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
CHAT_D = -900704
CHAT_E = -900705
CHAT_F = -900706


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


class _FakeErrorResponse:
    """Симулирует HTTP-уровневую ошибку cobalt/CDN (CR-02 06-REVIEW.md) —
    `raise_for_status()` поднимает `aiohttp.ClientResponseError` ДО того, как
    тело ответа могло бы быть прочитано/просчитано как валидный контент.
    `.json()`/`.content` намеренно падают с AssertionError, если что-то
    попробует прочитать их до вызова `raise_for_status()` — это гарантирует,
    что регрессионный тест ловит именно порядок вызовов, а не только факт
    поднятого исключения."""

    def __init__(self, status: int = 500) -> None:
        self._status = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def raise_for_status(self) -> None:
        raise aiohttp.ClientResponseError(
            request_info=SimpleNamespace(
                real_url="http://cobalt:9000/tunnel/abc", method="GET", headers={}
            ),
            history=(),
            status=self._status,
            message="Internal Server Error",
        )

    async def json(self):
        raise AssertionError("json() не должен вызываться до raise_for_status() (CR-02)")

    @property
    def content(self):
        raise AssertionError("content не должен читаться до raise_for_status() (CR-02)")


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


# --- CR-01 06-REVIEW.md: зарегистрированный фильтр хендлера (не только
# extract_url) должен матчить URL, встроенный НЕ первым токеном сообщения --


def test_registered_filter_matches_url_mid_message():
    """Регрессия на CR-01: `F.text.regexp(URL_RE)` БЕЗ `mode="search"`
    анкорит совпадение к позиции 0 (`pattern.match`) и никогда не срабатывает
    на реальные сообщения вида "смотри <ссылка>" — это единственный тест,
    который проверяет ИМЕННО зарегистрированный на роутере объект фильтра
    (`bot.handlers.media_dl.router`), а не `media_dl_service.extract_url()`
    напрямую (которая всегда использовала `.search()` и не ловила разницу)."""
    import bot.handlers.media_dl as media_dl_handler

    handlers = media_dl_handler.router.message.handlers
    assert len(handlers) == 1, "ожидается ровно один message-хендлер в этом роутере"
    magic_filter = handlers[0].filters[0].magic

    mid_message = SimpleNamespace(text="смотри https://vm.tiktok.com/xyz")
    assert magic_filter.resolve(mid_message) is not None, (
        "catch-all фильтр обязан матчить URL, даже если он не первый токен сообщения (D-08)"
    )

    bare_url = SimpleNamespace(text="https://vm.tiktok.com/xyz")
    assert magic_filter.resolve(bare_url) is not None

    no_url = SimpleNamespace(text="просто текст без ссылки")
    assert magic_filter.resolve(no_url) is None


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


# --- CR-02 06-REVIEW.md: non-2xx ответ не должен трактоваться как успех -----


@pytest.mark.asyncio
async def test_download_raises_on_non_2xx_status(monkeypatch):
    """Сломанный tunnel/CDN-ответ (не 2xx) обязан поднимать исключение ДО
    того, как тело начнёт стримиться и трактоваться как валидные байты файла
    — без этой проверки ошибка сервера тихо принималась бы за успешную
    загрузку и оплачивалась (D-07)."""
    from bot.services import media_dl_service

    fake_response = _FakeErrorResponse(status=500)
    fake_session = _FakeClientSession(get_response=fake_response)
    monkeypatch.setattr(aiohttp, "ClientSession", lambda *a, **kw: fake_session)

    with pytest.raises(aiohttp.ClientResponseError):
        await media_dl_service.download("http://cobalt:9000/tunnel/abc", max_bytes=2048)


@pytest.mark.asyncio
async def test_resolve_raises_on_non_2xx_status(monkeypatch):
    """Та же проверка (CR-02), что `test_download_raises_on_non_2xx_status`,
    для `resolve()` — HTTP-уровневая ошибка самого cobalt-сервиса (5xx/4xx,
    отдельно от application-level `status: "error"` внутри валидного
    200-ответа) не должна трактоваться как валидный JSON-ответ."""
    from bot.services import media_dl_service

    fake_response = _FakeErrorResponse(status=502)
    fake_session = _FakeClientSession(post_response=fake_response)
    monkeypatch.setattr(aiohttp, "ClientSession", lambda *a, **kw: fake_session)

    with pytest.raises(aiohttp.ClientResponseError):
        await media_dl_service.resolve("https://www.tiktok.com/@u/video/1")


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

    # debit_to_bank логирует 2 строки на один успешный вызов (нога игрока +
    # нога банка, тот же kind="mediadl_charge") — economy_service.py::debit_to_bank.
    assert await _count_tx(session, chat_id3, "mediadl_charge") == 2
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

    assert await _count_tx(session, chat_id3, "mediadl_charge") == 2
    assert (
        await _get_user_balance(session, chat_id3, user_id3)
        == settings.economy_start_bonus - settings.mediadl_cost
    )
    bot_mock3_retry.send_video.assert_not_awaited()
    assert ref_id  # ref_id формируется как f"mediadl:{message.message_id}" внутри хендлера


# --- WR-03 06-REVIEW.md: сетевые сбои cobalt/CDN не должны молча ронять апдейт --


@pytest.mark.asyncio
async def test_no_charge_when_resolve_raises_network_error(session, monkeypatch):
    """`resolve()` поднимает aiohttp.ClientError (сеть недоступна/таймаут) —
    хендлер обязан поймать это (WR-03), ответить пользователю по-русски и
    НЕ уронить апдейт необработанным исключением; денег это тоже не
    касается (списание ещё не начиналось)."""
    import bot.handlers.media_dl as media_dl
    from bot.services import media_dl_service

    chat_id, user_id = CHAT_D, 900704
    await _ensure_user(session, user_id)
    await economy_service.get_balance(session, chat_id, user_id)

    async def fake_resolve_network_error(url: str) -> dict:
        raise aiohttp.ClientConnectionError("connection refused")

    monkeypatch.setattr(media_dl_service, "resolve", fake_resolve_network_error)

    message = _fake_message(chat_id, user_id, "Тест", "https://www.tiktok.com/@u/video/4", message_id=104)
    bot_mock = AsyncMock()

    await media_dl.on_media_url(message, session, bot_mock)  # не должно поднять исключение

    assert await _count_tx(session, chat_id, "mediadl_charge") == 0
    assert await _get_user_balance(session, chat_id, user_id) == settings.economy_start_bonus
    bot_mock.send_video.assert_not_awaited()
    message.reply.assert_awaited_once()


@pytest.mark.asyncio
async def test_no_charge_when_download_raises_http_error(session, monkeypatch):
    """CR-02 + WR-03 06-REVIEW.md (комбинированный сценарий): cobalt резолвит
    tunnel успешно, но GET по tunnel-URL возвращает non-2xx — `download()`
    поднимает `aiohttp.ClientResponseError` (CR-02), хендлер обязан поймать
    это (WR-03) и НЕ вызывать `debit_to_bank` вовсе — сломанный tunnel не
    должен списывать деньги (D-07)."""
    import bot.handlers.media_dl as media_dl
    from bot.services import media_dl_service

    chat_id, user_id = CHAT_E, 900705
    await _ensure_user(session, user_id)
    await economy_service.get_balance(session, chat_id, user_id)

    async def fake_resolve_tunnel(url: str) -> dict:
        return {"status": "tunnel", "url": "http://cobalt:9000/tunnel/broken", "filename": "video.mp4"}

    async def fake_download_http_error(item_url: str, max_bytes: int) -> bytes | None:
        raise aiohttp.ClientResponseError(
            request_info=SimpleNamespace(real_url=item_url, method="GET", headers={}),
            history=(),
            status=502,
            message="Bad Gateway",
        )

    monkeypatch.setattr(media_dl_service, "resolve", fake_resolve_tunnel)
    monkeypatch.setattr(media_dl_service, "download", fake_download_http_error)

    message = _fake_message(chat_id, user_id, "Тест", "https://www.tiktok.com/@u/video/5", message_id=105)
    bot_mock = AsyncMock()

    await media_dl.on_media_url(message, session, bot_mock)  # не должно поднять исключение

    assert await _count_tx(session, chat_id, "mediadl_charge") == 0
    assert await _get_user_balance(session, chat_id, user_id) == settings.economy_start_bonus
    bot_mock.send_video.assert_not_awaited()
    message.reply.assert_awaited_once()


# --- WR-01 06-REVIEW.md: списание откатывается, если отправка в Telegram упала --


@pytest.mark.asyncio
async def test_charge_rolled_back_when_send_fails_then_retry_succeeds(session, monkeypatch):
    """`bot.send_video` падает ПОСЛЕ успешного скачивания — списание и
    отправка обёрнуты одним SAVEPOINT (`session.begin_nested()`), поэтому
    падение send_video откатывает ТОЛЬКО эту вложенную транзакцию, не всю
    сессию — деньги фактически не списываются, пользователь не остаётся без
    денег и без файла (WR-01 06-REVIEW.md). Ретрай того же апдейта (тот же
    message_id) с рабочей отправкой должен списать РОВНО ОДИН раз — откат
    SAVEPOINT освобождает `ref_id` для повторной попытки, а ранее
    закоммиченный стартовый баланс переживает откат (в отличие от
    session-wide `session.rollback()`, которая откатила бы и его)."""
    import bot.handlers.media_dl as media_dl
    from bot.services import media_dl_service

    chat_id, user_id = CHAT_F, 900706
    await _ensure_user(session, user_id)
    await economy_service.get_balance(session, chat_id, user_id)

    async def fake_resolve_tunnel(url: str) -> dict:
        return {"status": "tunnel", "url": "http://cobalt:9000/tunnel/abc", "filename": "video.mp4"}

    async def fake_download(item_url: str, max_bytes: int) -> bytes:
        return b"fake-video-bytes"

    monkeypatch.setattr(media_dl_service, "resolve", fake_resolve_tunnel)
    monkeypatch.setattr(media_dl_service, "download", fake_download)

    message_id = 106
    message = _fake_message(chat_id, user_id, "Тест", "https://www.tiktok.com/@u/video/6", message_id=message_id)
    bot_mock_failing = AsyncMock()
    bot_mock_failing.send_video.side_effect = RuntimeError("simulated Telegram upload failure")

    await media_dl.on_media_url(message, session, bot_mock_failing)

    # Списание НЕ закоммичено (откачено) — ни одной строки в economy_tx,
    # ранее закоммиченный стартовый баланс не тронут.
    assert await _count_tx(session, chat_id, "mediadl_charge") == 0
    assert await _get_user_balance(session, chat_id, user_id) == settings.economy_start_bonus
    message.reply.assert_awaited_once()

    # Ретрай того же апдейта (тот же message_id) с рабочей отправкой —
    # откат SAVEPOINT освободил ref_id, поэтому списание проходит РОВНО один раз.
    message_retry = _fake_message(
        chat_id, user_id, "Тест", "https://www.tiktok.com/@u/video/6", message_id=message_id
    )
    bot_mock_ok = AsyncMock()
    await media_dl.on_media_url(message_retry, session, bot_mock_ok)

    assert await _count_tx(session, chat_id, "mediadl_charge") == 2  # игрок + банк
    assert (
        await _get_user_balance(session, chat_id, user_id)
        == settings.economy_start_bonus - settings.mediadl_cost
    )
    bot_mock_ok.send_video.assert_awaited_once()


# --- дневной лимит на пользователя (возврат скачивания в группы) -----------


@pytest.mark.asyncio
async def test_count_today_scoped_to_user_chat_and_excludes_bank_leg(session):
    """`count_today` считает ТОЛЬКО строки данного (chat_id, user_id) с
    kind="mediadl_charge" — банковское зеркало той же операции (`user_id IS
    NULL`, `debit_to_bank` пишет обе ноги одним kind) и строки другого
    пользователя/чата в счёт попадать не должны."""
    from bot.services import media_dl_service

    chat_id, user_id, other_user_id = -900801, 900801, 900802
    await _ensure_user(session, user_id)
    await _ensure_user(session, other_user_id)
    await economy_service.get_balance(session, chat_id, user_id)
    await economy_service.get_balance(session, chat_id, other_user_id)

    await economy_service.debit_to_bank(
        session, chat_id, user_id, 10, kind="mediadl_charge", ref_id="mediadl:t1"
    )
    await economy_service.debit_to_bank(
        session, chat_id, user_id, 10, kind="mediadl_charge", ref_id="mediadl:t2"
    )
    # Другой пользователь в том же чате — не должен попадать в счёт первого.
    await economy_service.debit_to_bank(
        session, chat_id, other_user_id, 10, kind="mediadl_charge", ref_id="mediadl:t3"
    )
    await session.commit()

    assert await media_dl_service.count_today(session, chat_id, user_id) == 2
    assert await media_dl_service.count_today(session, chat_id, other_user_id) == 1
    # Чат без единой операции — ноль, а не ошибка.
    assert await media_dl_service.count_today(session, -900899, user_id) == 0


@pytest.mark.asyncio
async def test_daily_limit_blocks_before_resolve_and_does_not_charge(session, monkeypatch):
    """Исчерпанный дневной лимит обязан остановить хендлер ДО вызова
    `resolve()` (не тратить впустую запрос к cobalt на заведомо отклонённую
    попытку) и не двигать деньги вовсе. Лимит понижен монкипатчем до 2, чтобы
    не гонять реальных 5 успешных загрузок в тесте."""
    import bot.handlers.media_dl as media_dl
    from bot.services import media_dl_service

    monkeypatch.setattr(settings, "mediadl_daily_limit", 2)

    chat_id, user_id = -900802, 900803
    await _ensure_user(session, user_id)
    await economy_service.get_balance(session, chat_id, user_id)

    async def fake_resolve_tunnel(url: str) -> dict:
        return {"status": "tunnel", "url": "http://cobalt:9000/tunnel/abc", "filename": "video.mp4"}

    async def fake_download(item_url: str, max_bytes: int) -> bytes:
        return b"fake-video-bytes"

    monkeypatch.setattr(media_dl_service, "resolve", fake_resolve_tunnel)
    monkeypatch.setattr(media_dl_service, "download", fake_download)

    # Два успешных скачивания подряд — ровно на пороге лимита (2).
    for message_id in (201, 202):
        message = _fake_message(
            chat_id, user_id, "Тест", "https://www.tiktok.com/@u/video/x", message_id=message_id
        )
        bot_mock = AsyncMock()
        await media_dl.on_media_url(message, session, bot_mock)
        bot_mock.send_video.assert_awaited_once()

    assert await media_dl_service.count_today(session, chat_id, user_id) == 2
    balance_after_two = await _get_user_balance(session, chat_id, user_id)
    assert balance_after_two == settings.economy_start_bonus - 2 * settings.mediadl_cost

    # Третья попытка — лимит исчерпан, resolve() вообще не должен вызваться.
    async def fail_if_called(url: str) -> dict:
        raise AssertionError("resolve() не должен вызываться после исчерпания дневного лимита")

    monkeypatch.setattr(media_dl_service, "resolve", fail_if_called)

    message3 = _fake_message(
        chat_id, user_id, "Тест", "https://www.tiktok.com/@u/video/x", message_id=203
    )
    bot_mock3 = AsyncMock()
    await media_dl.on_media_url(message3, session, bot_mock3)

    bot_mock3.send_video.assert_not_awaited()
    message3.reply.assert_awaited_once()
    assert str(settings.mediadl_daily_limit) in message3.reply.await_args.args[0]
    # Деньги за отклонённую попытку не двигались — баланс не изменился.
    assert await _get_user_balance(session, chat_id, user_id) == balance_after_two
