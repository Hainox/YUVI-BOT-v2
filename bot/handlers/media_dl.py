"""Catch-all URL-хендлер скачивания медиа (MEDIA-01/02, D-05..D-08).

Тонкий хендлер (форма bot/handlers/duel.py): парсит вход, зовёт
`media_dl_service` (resolve/download) и `economy_service.debit_to_bank`,
форматирует ответ. Единственная точка списания — `debit_to_bank`, вызывается
СТРОГО после успешной загрузки (D-07, charge-only-on-success) — при ошибке
cobalt/сети, `local-processing` или превышении `MEDIADL_MAX_MB` деньги не
двигаются вовсе.

Фильтр `F.text.regexp(media_dl_service.URL_RE, mode="search")` срабатывает
на сообщения с распознанной ссылкой ГДЕ УГОДНО в тексте (не только первым
токеном — `mode="search"` обязателен, дефолтный `mode="match"` у
`magic_filter`/aiogram анкорит поиск к позиции 0 и пропускает подавляющее
большинство реальных сообщений вида "смотри <ссылка>", D-08). SSRF-whitelist
уже внутри самого `URL_RE` (T-06-03) — существующие `Command(...)`-хендлеры
не задевает, формы фильтров взаимоисключающие (текст команды начинается с
`/`, а не с `http(s)://`, а совпадение `URL_RE` требует `http(s)://` где-то в
тексте). Router подхватывается `bot/main.py::_discover_routers` автоматически.

Списание (`economy_service.debit_to_bank`) и отправка файла в Telegram
обёрнуты ОДНИМ SAVEPOINT (`session.begin_nested()`, форма
`economy_service.py::transfer_with_fee`): если `bot.send_video`/
`send_media_group` падает, откатывается ТОЛЬКО эта вложенная транзакция
(списание), не вся сессия целиком — деньги фактически применяются, только
когда доставка подтверждена (D-07 расширен на шаг доставки, не только на шаг
скачивания, WR-01 06-REVIEW.md). Это же сохраняет идемпотентность на повторе
апдейта: `debit_to_bank` проверяет `ref_id` ДО отправки, поэтому повторный
вызов с тем же `message_id` не шлёт файл повторно.
"""

from __future__ import annotations

import asyncio
import logging

import aiohttp
from aiogram import Bot
from aiogram import F
from aiogram import Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.exceptions import TelegramForbiddenError
from aiogram.types import BufferedInputFile
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import settings
from bot.services import economy_service
from bot.services import media_dl_service

logger = logging.getLogger(__name__)

router = Router()

_MAX_BYTES = settings.mediadl_max_mb * 1024 * 1024
_SIZE_ERROR = f"Файл больше {settings.mediadl_max_mb}МБ — скачивание отменено."
_EMPTY_RESULT_ERROR = "Не удалось скачать медиа по этой ссылке. Попробуйте другую ссылку."
_RESOLVE_ERROR = "Не удалось обработать ссылку — сервис скачивания временно недоступен. Попробуйте позже."
_DOWNLOAD_ERROR = "Не удалось скачать файл по ссылке — возможно, она устарела. Попробуйте отправить ссылку заново."
_SEND_ERROR = "Не удалось отправить файл в чат — попробуйте ещё раз, деньги не списаны."

# Сетевые/протокольные сбои cobalt-клиента (WR-03): aiohttp.ClientError не
# покрывает asyncio.TimeoutError (total-таймаут ClientTimeout поднимает его
# напрямую, не оборачивая) — ловим обе группы явно.
_NETWORK_ERRORS = (aiohttp.ClientError, asyncio.TimeoutError)


async def _delete_source_message(bot: Bot, message: Message) -> None:
    """Удаляет исходное сообщение со ссылкой ПОСЛЕ успешной отправки видео —
    иначе в чате остаются и нативный Telegram-превью ссылки, и чистое видео
    от бота (дубль контента). Best-effort (форма tag_service.py/
    pinned_menu_service.py): без права can_delete_messages у бота удаление
    молча не сработает, остальной флоу (доставка+списание уже подтверждены)
    от этого не откатывается."""
    try:
        await bot.delete_message(message.chat.id, message.message_id)
    except (TelegramBadRequest, TelegramForbiddenError):
        logger.warning(
            "media_dl: не удалось удалить исходное сообщение chat_id=%s message_id=%s "
            "(боту не хватает can_delete_messages?)",
            message.chat.id,
            message.message_id,
        )


async def _download_or_reply(message: Message, item_url: str) -> bytes | None:
    try:
        file_bytes = await media_dl_service.download(item_url, _MAX_BYTES)
    except _NETWORK_ERRORS:
        logger.warning("media_dl: download() failed for item_url=%s", item_url, exc_info=True)
        await message.reply(_DOWNLOAD_ERROR)
        return None
    if file_bytes is None:
        await message.reply(_SIZE_ERROR)
    return file_bytes


@router.message(
    F.chat.type == "private",  # TEMPORARY: cobalt-скачивание временно только в ЛС, не в группе — откатить, убрав этот фильтр
    F.text.regexp(media_dl_service.URL_RE, mode="search"),
)
async def on_media_url(message: Message, session: AsyncSession, bot: Bot) -> None:
    if message.from_user is None or message.text is None:
        return

    url = media_dl_service.extract_url(message.text)
    if url is None:
        return

    try:
        result = await media_dl_service.resolve(url)
    except _NETWORK_ERRORS:
        logger.warning("media_dl: resolve() failed for url=%s", url, exc_info=True)
        await message.reply(_RESOLVE_ERROR)
        return
    status = result.get("status")

    if status in ("error", "local-processing"):
        await message.reply(media_dl_service.map_error(result))
        return

    chat_id = message.chat.id
    user_id = message.from_user.id
    ref_id = f"mediadl:{message.message_id}"

    if status in ("tunnel", "redirect"):
        item_url = result.get("url")
        if not item_url:
            await message.reply(_EMPTY_RESULT_ERROR)
            return

        file_bytes = await _download_or_reply(message, item_url)
        if file_bytes is None:
            return

        filename = result.get("filename") or "video.mp4"
        debited = False
        try:
            # SAVEPOINT (форма economy_service.py::transfer_with_fee) — если
            # send_video упадёт, `begin_nested()` автоматически откатит
            # ТОЛЬКО эту вложенную транзакцию (списание), не затрагивая
            # остальную сессию (WR-01 06-REVIEW.md). Списание фактически
            # применяется, только когда доставка подтверждена (D-07 расширен
            # на шаг доставки).
            async with session.begin_nested():
                debited = await economy_service.debit_to_bank(
                    session, chat_id, user_id, settings.mediadl_cost, kind="mediadl_charge", ref_id=ref_id
                )
                if debited:
                    await bot.send_video(chat_id, BufferedInputFile(file_bytes, filename=filename))
        except Exception:
            logger.exception(
                "media_dl: send_video упал после списания (ref_id=%s), списание отменено", ref_id
            )
            await message.reply(_SEND_ERROR)
            return

        if not debited:
            logger.info("media_dl: ref_id=%s уже применён, повтор пропущен", ref_id)
            return

        await session.commit()
        await _delete_source_message(bot, message)
        return

    if status == "picker":
        items = media_dl_service.cap_picker(result.get("picker") or [])
        if not items:
            await message.reply(_EMPTY_RESULT_ERROR)
            return

        media_group = []
        for entry in items:
            entry_bytes = await _download_or_reply(message, entry.get("url", ""))
            if entry_bytes is None:
                return
            media_cls = media_dl_service.picker_media_class(entry.get("type"))
            filename = entry.get("filename") or "media"
            media_group.append(media_cls(media=BufferedInputFile(entry_bytes, filename=filename)))

        debited = False
        try:
            async with session.begin_nested():
                debited = await economy_service.debit_to_bank(
                    session, chat_id, user_id, settings.mediadl_cost, kind="mediadl_charge", ref_id=ref_id
                )
                if debited:
                    await bot.send_media_group(chat_id, media_group)
        except Exception:
            logger.exception(
                "media_dl: send_media_group упал после списания (ref_id=%s), списание отменено",
                ref_id,
            )
            await message.reply(_SEND_ERROR)
            return

        if not debited:
            logger.info("media_dl: ref_id=%s уже применён, повтор пропущен", ref_id)
            return

        await session.commit()
        await _delete_source_message(bot, message)
        return

    # Неизвестный статус cobalt — трактуем как отказ, деньги не двигаем.
    await message.reply(media_dl_service.map_error(result))
