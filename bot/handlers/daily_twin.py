"""Реакция "дневного двойника" (TWIN-03, bot/services/daily_twin_service.py)
на реплаи к его же постам.

Фильтр `F.reply_to_message` (коарс, матчит ЛЮБОЙ реплай) + защитная
дозафильтровка ВНУТРИ хендлера (`message.text is None or .startswith("/")` —
не текст/команда-как-реплай — не наш случай) — та же форма, что
`bot/handlers/media_dl.py::on_media_url` (коарс-фильтр магии + повторная
проверка условий в теле). В отличие от media_dl.py, здесь позитивное условие
(это реплай) НЕ структурно взаимоисключающе с командами (реплай на сообщение
может одновременно быть текстом команды) — поэтому явно исключаем `/`-тексты
в теле, ДО любого обращения к БД, чтобы не перехватывать апдейты, которые
на самом деле адресованы Command(...)-хендлерам, зарегистрированным позже
(`bot/main.py::_discover_routers` подключает роутеры в алфавитном порядке
имени модуля — "daily_twin" раньше многих команд).

Само распознавание "это реплай ИМЕННО на пост двойника" — не через
`reply_to_message.from_user.id == bot.id` (это отличило бы "любое сообщение
бота", а не конкретно посты двойника), а через
`daily_twin_service.find_target_by_post` — собственный журнал
`daily_twin_posts` (сообщения бота вообще не попадают в `messages`, см.
докстринг `common/models/daily_twin_post.py`). Обычный реплай на любое другое
сообщение чата просто не находит совпадения и хендлер тихо ничего не делает —
дальше этого апдейта в дальнейшую обработку он не идёт, но и не отправляет
ничего в чат.

Ошибки — тихий скип (лог, без ответа в чат): это фоновая, не команда-по-
запросу механика, никто явно не ждёт гарантированного ответа именно сейчас
(та же дисциплина, что lurker_service — тик обязан пережить любую ошибку).
"""

from __future__ import annotations

import html
import logging

from aiogram import F
from aiogram import Router
from aiogram.types import Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.services import daily_twin_service
from bot.services import twin_service
from common.models.user import User

logger = logging.getLogger(__name__)
router = Router()


@router.message(F.reply_to_message)
async def daily_twin_reaction(message: Message, session: AsyncSession) -> None:
    if (
        message.reply_to_message is None
        or message.from_user is None
        or message.text is None
        or message.text.startswith("/")
    ):
        return

    target_user_id = await daily_twin_service.find_target_by_post(
        session, message.chat.id, message.reply_to_message.message_id
    )
    if target_user_id is None:
        return  # обычный реплай не на пост двойника — не наш случай

    name = (
        await session.execute(select(User.first_name).where(User.id == target_user_id))
    ).scalar_one_or_none() or str(target_user_id)

    try:
        raw_text = await twin_service.build_twin_reaction(
            session, message.chat.id, target_user_id, name, message.text
        )
    except twin_service.TwinConsentError:
        # Согласие отозвано (/twin_pause) с момента поста — тихо молчим, не
        # объясняем механику явным сообщением (фон, не команда-по-запросу).
        return
    except Exception:  # noqa: BLE001 - фон обязан пережить любую ошибку, см. докстринг
        logger.exception(
            "daily_twin_reaction: build_twin_reaction упал chat_id=%s target_user_id=%s",
            message.chat.id,
            target_user_id,
        )
        return

    if raw_text == twin_service.TWIN_FALLBACK_TEXT:
        return  # не постим заглушку в чат ЯКОБЫ от лица человека

    # D-02/Pitfall 8: дисклеймер хардкожен ЗДЕСЬ, тот же принцип, что и у
    # bot/handlers/twin.py — сервис никогда не отдаёт префикс на генерацию модели.
    text = f"🎭 Двойник дня — {html.escape(name)}: {html.escape(raw_text)}"
    sent = await message.answer(text, parse_mode="HTML")
    await daily_twin_service.record_post(session, message.chat.id, sent.message_id, target_user_id)
    await session.commit()
