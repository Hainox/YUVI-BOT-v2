"""Catch-all URL-хендлер скачивания медиа (MEDIA-01/02, D-05..D-08).

Тонкий хендлер (форма bot/handlers/duel.py): парсит вход, зовёт
`media_dl_service` (resolve/download) и `economy_service.debit_to_bank`,
форматирует ответ. Единственная точка списания — `debit_to_bank`, вызывается
СТРОГО после успешной загрузки (D-07, charge-only-on-success) — при ошибке
cobalt/сети, `local-processing` или превышении `MEDIADL_MAX_MB` деньги не
двигаются вовсе.

Фильтр `F.text.regexp(media_dl_service.URL_RE)` срабатывает только на
сообщения с распознанной ссылкой (SSRF-whitelist уже внутри самого
`URL_RE`, T-06-03) — существующие `Command(...)`-хендлеры не задевает,
формы фильтров взаимоисключающие (текст команды начинается с `/`, а не с
`http(s)://`). Router подхватывается `bot/main.py::_discover_routers`
автоматически.
"""

from __future__ import annotations

import logging

from aiogram import Bot
from aiogram import F
from aiogram import Router
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


async def _download_or_reply(message: Message, item_url: str) -> bytes | None:
    file_bytes = await media_dl_service.download(item_url, _MAX_BYTES)
    if file_bytes is None:
        await message.reply(_SIZE_ERROR)
    return file_bytes


@router.message(F.text.regexp(media_dl_service.URL_RE))
async def on_media_url(message: Message, session: AsyncSession, bot: Bot) -> None:
    if message.from_user is None or message.text is None:
        return

    url = media_dl_service.extract_url(message.text)
    if url is None:
        return

    result = await media_dl_service.resolve(url)
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

        debited = await economy_service.debit_to_bank(
            session, chat_id, user_id, settings.mediadl_cost, kind="mediadl_charge", ref_id=ref_id
        )
        if not debited:
            logger.info("media_dl: ref_id=%s уже применён, повтор пропущен", ref_id)
            return
        await session.commit()

        filename = result.get("filename") or "video.mp4"
        await bot.send_video(chat_id, BufferedInputFile(file_bytes, filename=filename))
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

        debited = await economy_service.debit_to_bank(
            session, chat_id, user_id, settings.mediadl_cost, kind="mediadl_charge", ref_id=ref_id
        )
        if not debited:
            logger.info("media_dl: ref_id=%s уже применён, повтор пропущен", ref_id)
            return
        await session.commit()

        await bot.send_media_group(chat_id, media_group)
        return

    # Неизвестный статус cobalt — трактуем как отказ, деньги не двигаем.
    await message.reply(media_dl_service.map_error(result))
