"""Реакция "дневного двойника" (TWIN-03, bot/services/daily_twin_service.py):

1. Реплай на пост, который САМ бот отправил от имени двойника (проактивный
   пост ИЛИ более ранний ответ) — работает для персоны ЛЮБОГО прошлого дня
   (см. `daily_twin_posts`), сохраняет непрерывность треда, даже если
   сегодняшняя персона уже другая.
2. Реплай на РЕАЛЬНОЕ сообщение сегодняшней персоны (её настоящий, не
   ботовый пост) — расширено 2026-07-28 по запросу пользователя.
3. @упоминание сегодняшней персоны (`text_mention` — Telegram уже резолвит
   в user-объект; либо plain `@username`, матчится подстрокой из
   message.text) — та же дата расширения.

(2)/(3) осознанно scoped ТОЛЬКО на сегодняшнюю персону (не на "любого, кто
когда-либо был двойником") — вне контекста своего дня реагировать от лица
человека, который сейчас уже не является активной персоной, не имеет смысла
(см. модульный докстринг daily_twin_service.py: пик персоны дня — не
"вечное" согласие, оно живёт один MSK-день). Само упоминание/реплай не
проверяет consent заранее — просто резолвит target_user_id; согласие
(`TwinConsentError`) проверяется ВНУТРИ `twin_service.build_twin_reaction`,
та же дисциплина, что и у проактивного тика.

Роутинг (важно): фильтр `F.reply_to_message | F.entities` — заметно шире
простого "это реплай", потому что теперь ловим ещё и упоминания в ЛЮБОМ
сообщении (не обязательно реплай). Это пересекается с сообщениями, которые
на самом деле адресованы ДРУГИМ хендлерам — например `/twin @username`
(команда + text_mention entity одновременно) или реплай с TikTok-ссылкой
(`media_dl.py`'s `F.text.regexp(URL_RE)`). Поэтому КАЖДЫЙ ранний выход,
который означает "это сообщение не для нас", делает `raise SkipHandler`
(aiogram.dispatcher.event.bases) вместо тихого `return` — SkipHandler
явно говорит диспетчеру "не считай апдейт обработанным, пробуй следующий
роутер" (`bot/main.py::_discover_routers` подключает по одному роутеру на
файл в алфавитном порядке имени модуля — "daily_twin" раньше почти всех
Command(...)-хендлеров и раньше `media_dl.py`). Обычный `return` (не
SkipHandler) используется ТОЛЬКО после того, как мы уже точно определили,
что апдейт — наш (нашли реального target_user_id), но дальше что-то пошло
не так (согласие отозвано / AI не ответил) — в этом случае апдейт
действительно наш, просто молча ничего не постим, а не отдаём другим
хендлерам сообщение, которое явно было адресовано двойнику.
"""

from __future__ import annotations

import logging

from aiogram import F
from aiogram import Router
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.types import Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import settings
from bot.services import daily_twin_service
from bot.services import twin_service
from common.models.user import User

logger = logging.getLogger(__name__)
router = Router()


def _mentions_user(message: Message, user_id: int, username: str | None) -> bool:
    """True, если entities сообщения содержат `text_mention` на user_id
    (Telegram уже резолвил в user-объект — надёжно) ИЛИ plain `@username`
    (нет user-объекта, только текстовая подстрока — матчим по
    offset/length из entity, Telegram не резолвит plain-упоминания сам)."""
    if not message.entities or message.text is None:
        return False
    for entity in message.entities:
        if entity.type == "text_mention" and entity.user is not None and entity.user.id == user_id:
            return True
        if entity.type == "mention" and username is not None:
            mentioned = message.text[entity.offset : entity.offset + entity.length].lstrip("@")
            if mentioned.lower() == username.lower():
                return True
    return False


@router.message(F.reply_to_message | F.entities)
async def daily_twin_reaction(message: Message, session: AsyncSession) -> None:
    if message.from_user is None or message.text is None or message.text.startswith("/"):
        raise SkipHandler

    # (1) Реплай на пост САМОГО бота (любой прошлый день) — приоритет
    # первым, это самый однозначный сигнал.
    target_user_id: int | None = None
    display_name: str | None = None
    if message.reply_to_message is not None:
        target_user_id = await daily_twin_service.find_target_by_post(
            session, message.chat.id, message.reply_to_message.message_id
        )

    # (2)/(3) Реплай на РЕАЛЬНОЕ сообщение сегодняшней персоны, ИЛИ её
    # @упоминание — только если (1) не сработал (не тратим DB-чтение
    # get_todays_twin на сообщения, где уже нашли ответ).
    if target_user_id is None:
        replied_real_person_id = (
            message.reply_to_message.from_user.id
            if message.reply_to_message is not None and message.reply_to_message.from_user is not None
            else None
        )
        has_mention_entity = message.entities is not None and any(
            e.type in ("mention", "text_mention") for e in message.entities
        )
        if replied_real_person_id is not None or has_mention_entity:
            twin = await daily_twin_service.get_todays_twin(session, message.chat.id)
            if twin is not None:
                twin_user_id, twin_name, twin_username = twin
                # Сама персона комментирует/упоминает себя — не отвечаем
                # сами себе её же голосом, это её собственные слова.
                is_self = message.from_user.id == twin_user_id
                if not is_self and (
                    replied_real_person_id == twin_user_id
                    or _mentions_user(message, twin_user_id, twin_username)
                ):
                    target_user_id = twin_user_id
                    display_name = twin_name

    if target_user_id is None:
        raise SkipHandler

    # Тот же жёсткий суточный потолок, что и у проактивного тика
    # (`daily_twin_service.maybe_post_proactively`) — БЕЗ него реактивная
    # ветка не имела вообще никакого ограничения: если сегодняшняя персона
    # оказывается активным участником, на которого часто отвечают/которого
    # часто упоминают, бот генерировал ответ на КАЖДЫЙ такой апдейт без
    # перерыва (живой инцидент 2026-07-29 — воспринималось как спам).
    # `count_todays_posts` уже считает посты ОБОИХ путей (одна и та же
    # таблица `daily_twin_posts`), поэтому проверка здесь и там делят один
    # и тот же бюджет на день, а не два независимых. Апдейт уже точно наш
    # (target_user_id найден) — молчим обычным `return`, не `SkipHandler`.
    posts_so_far = await daily_twin_service.count_todays_posts(session, message.chat.id)
    if posts_so_far >= settings.daily_twin_max_posts:
        return

    if display_name is None:
        display_name = (
            await session.execute(select(User.first_name).where(User.id == target_user_id))
        ).scalar_one_or_none() or str(target_user_id)

    try:
        raw_text = await twin_service.build_twin_reaction(
            session, message.chat.id, target_user_id, display_name, message.text
        )
    except twin_service.TwinConsentError:
        # Согласие отозвано/на паузе — тихо молчим (фон, не команда-по-
        # запросу), апдейт уже наш (не SkipHandler — см. модульный докстринг).
        return
    except Exception:  # noqa: BLE001 - фон обязан пережить любую ошибку
        logger.exception(
            "daily_twin_reaction: build_twin_reaction упал chat_id=%s target_user_id=%s",
            message.chat.id,
            target_user_id,
        )
        return

    if raw_text == twin_service.TWIN_FALLBACK_TEXT:
        return  # не постим заглушку в чат ЯКОБЫ от лица человека

    # Голый текст, БЕЗ дисклеймер-префикса (запрошено 2026-07-28, явный отказ
    # от D-02/Pitfall 8 — владелец бота осознанно выбрал неразмеченный ответ
    # вместо "🎭 Двойник дня — Имя:"). parse_mode не нужен — раньше HTML был
    # только ради разметки префикса, сырой AI-текст отправляется как есть.
    sent = await message.answer(raw_text)
    await daily_twin_service.record_post(session, message.chat.id, sent.message_id, target_user_id)
    await session.commit()
