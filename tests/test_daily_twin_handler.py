"""Тесты реакции "дневного двойника" на реплаи/упоминания (TWIN-03,
bot/handlers/daily_twin.py) — против живого Postgres (фикстура `session`).

Доказывают:
- Не-реплай-без-упоминаний / реплай-команда (текст с `/`) / реплай на ЧУЖОЕ
  (не двойника) сообщение / упоминание постороннего — хендлер отдаёт апдейт
  дальше через `SkipHandler` (не считает его "своим", не шлёт в чат) —
  критично для роутинга (см. модульный докстринг daily_twin.py): без этого
  сломались бы Command(...)-хендлеры и media_dl.py на пересекающихся
  апдейтах (например `/twin @username` — команда + text_mention entity).
- Реплай ИМЕННО на пост двойника (найден в daily_twin_posts) — генерирует
  контекстный ответ через twin_service.build_twin_reaction и отправляет его
  ГОЛЫМ текстом, без дисклеймер-префикса (явный отказ от D-02/Pitfall 8,
  запрошено 2026-07-28 — владелец бота осознанно выбрал неразмеченный ответ),
  журналирует СВОЙ ответ тоже (чтобы цепочка реплаев продолжалась).
- Реплай на РЕАЛЬНОЕ сообщение сегодняшней персоны и её @упоминание
  (text_mention И plain @username) — та же реакция, расширено 2026-07-28.
- Сама персона комментирует/упоминает себя — не реагирует (не отвечает
  своим же голосом самой себе).
- Отозванное согласие (TwinConsentError) и AI-фолбэк (TWIN_FALLBACK_TEXT) —
  тихий скип (обычный return, апдейт уже "наш"), без ответа в чат.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.dispatcher.event.bases import SkipHandler

import bot.handlers.daily_twin as daily_twin_handlers
from bot.services import daily_twin_service
from bot.services import twin_service
from common.models.twin_opt_in import TwinOptIn
from common.models.user import User


async def _ensure_user(session, user_id: int, first_name: str = "Тест", username: str | None = None) -> None:
    session.add(User(id=user_id, first_name=first_name, username=username))
    await session.flush()


async def _set_opt_in(session, chat_id: int, user_id: int, status: str) -> None:
    session.add(TwinOptIn(chat_id=chat_id, user_id=user_id, status=status))
    await session.flush()


async def _fake_stream(messages: list[dict], model: str, max_tokens: int) -> AsyncIterator[str]:
    yield "норм чё"


async def _raising_stream(messages: list[dict], model: str, max_tokens: int) -> AsyncIterator[str]:
    raise RuntimeError("Модель вернула только reasoning без ответа")
    yield  # pragma: no cover - unreachable, делает функцию async-генератором


def _entity(entity_type: str, offset: int, length: int, user: SimpleNamespace | None = None):
    return SimpleNamespace(type=entity_type, offset=offset, length=length, user=user)


def _fake_message(
    chat_id: int,
    user_id: int,
    text: str | None,
    *,
    reply_to_message_id: int | None = None,
    reply_to_from_user_id: int | None = None,
    entities: list | None = None,
    sent_message_id: int = 700_001,
):
    reply_to = None
    if reply_to_message_id is not None:
        reply_to = SimpleNamespace(
            message_id=reply_to_message_id,
            from_user=SimpleNamespace(id=reply_to_from_user_id) if reply_to_from_user_id is not None else None,
        )
    return SimpleNamespace(
        chat=SimpleNamespace(id=chat_id),
        from_user=SimpleNamespace(id=user_id, first_name="Кто-то"),
        text=text,
        reply_to_message=reply_to,
        entities=entities,
        answer=AsyncMock(return_value=SimpleNamespace(message_id=sent_message_id)),
    )


# --- no-op ветки (SkipHandler — апдейт не наш) -------------------------------


@pytest.mark.asyncio
async def test_ignores_plain_message(session):
    message = _fake_message(-100941001, 941001, "просто текст")

    with pytest.raises(SkipHandler):
        await daily_twin_handlers.daily_twin_reaction(message, session)

    message.answer.assert_not_awaited()


@pytest.mark.asyncio
async def test_ignores_command_reply(session):
    message = _fake_message(-100941002, 941002, "/twin", reply_to_message_id=800_001)

    with pytest.raises(SkipHandler):
        await daily_twin_handlers.daily_twin_reaction(message, session)

    message.answer.assert_not_awaited()


@pytest.mark.asyncio
async def test_ignores_non_text_reply(session):
    message = _fake_message(-100941003, 941003, None, reply_to_message_id=800_002)

    with pytest.raises(SkipHandler):
        await daily_twin_handlers.daily_twin_reaction(message, session)

    message.answer.assert_not_awaited()


@pytest.mark.asyncio
async def test_ignores_reply_to_unrelated_message(session):
    """Реплай на реальное сообщение чата, но это НЕ пост двойника
    (в daily_twin_posts такого telegram_message_id нет) и отправитель
    реплая — не сегодняшняя персона (кандидатов на сегодня вообще нет)."""
    message = _fake_message(-100941004, 941004, "го ещё раз", reply_to_message_id=800_003, reply_to_from_user_id=941099)

    with pytest.raises(SkipHandler):
        await daily_twin_handlers.daily_twin_reaction(message, session)

    message.answer.assert_not_awaited()


@pytest.mark.asyncio
async def test_ignores_mention_of_unrelated_user(session):
    chat_id = -100941011
    await _ensure_user(session, 941011, "Кто-то", username="kто_то")
    message = _fake_message(
        chat_id,
        941012,
        "@kто_то как дела",
        entities=[_entity("mention", 0, len("@kто_то"))],
    )

    with pytest.raises(SkipHandler):
        await daily_twin_handlers.daily_twin_reaction(message, session)

    message.answer.assert_not_awaited()


# --- реплай на пост двойника ---------------------------------------------------


@pytest.mark.asyncio
async def test_reacts_and_records_own_reply(session, monkeypatch):
    monkeypatch.setattr(twin_service.ai_client, "stream", _fake_stream)

    chat_id = -100941005
    target_id = 941005
    twin_post_message_id = 800_004
    await _ensure_user(session, target_id, "Дима")
    await _set_opt_in(session, chat_id, target_id, "active")
    await daily_twin_service.record_post(session, chat_id, twin_post_message_id, target_id)
    await session.commit()

    message = _fake_message(
        chat_id, 941006, "го ещё раз", reply_to_message_id=twin_post_message_id, sent_message_id=800_005
    )

    await daily_twin_handlers.daily_twin_reaction(message, session)

    message.answer.assert_awaited_once()
    text = message.answer.await_args.args[0]
    assert text == "норм чё"

    # Собственный ответ тоже зажурналирован — цепочка реплаев может продолжаться.
    found = await daily_twin_service.find_target_by_post(session, chat_id, 800_005)
    assert found == target_id


@pytest.mark.asyncio
async def test_silent_on_consent_revoked(session, monkeypatch):
    monkeypatch.setattr(twin_service.ai_client, "stream", _fake_stream)

    chat_id = -100941007
    target_id = 941007
    twin_post_message_id = 800_006
    await _ensure_user(session, target_id, "Пауза")
    await _set_opt_in(session, chat_id, target_id, "paused")
    await daily_twin_service.record_post(session, chat_id, twin_post_message_id, target_id)
    await session.commit()

    message = _fake_message(chat_id, 941008, "эй", reply_to_message_id=twin_post_message_id)

    await daily_twin_handlers.daily_twin_reaction(message, session)

    message.answer.assert_not_awaited()


@pytest.mark.asyncio
async def test_silent_on_ai_fallback(session, monkeypatch):
    monkeypatch.setattr(twin_service.ai_client, "stream", _raising_stream)

    chat_id = -100941009
    target_id = 941009
    twin_post_message_id = 800_007
    await _ensure_user(session, target_id, "Молчун")
    await _set_opt_in(session, chat_id, target_id, "active")
    await daily_twin_service.record_post(session, chat_id, twin_post_message_id, target_id)
    await session.commit()

    message = _fake_message(chat_id, 941010, "как дела", reply_to_message_id=twin_post_message_id)

    await daily_twin_handlers.daily_twin_reaction(message, session)

    message.answer.assert_not_awaited()


# --- расширение 2026-07-28: реплай на РЕАЛЬНОЕ сообщение / @упоминание ------


@pytest.mark.asyncio
async def test_reacts_to_reply_on_real_message_of_todays_twin(session, monkeypatch):
    monkeypatch.setattr(twin_service.ai_client, "stream", _fake_stream)

    chat_id = -100941013
    target_id = 941013
    await _ensure_user(session, target_id, "Дима")
    await _set_opt_in(session, chat_id, target_id, "active")
    await session.commit()

    # Реплай на РЕАЛЬНОЕ сообщение (from_user.id == target_id), не на пост
    # бота — daily_twin_posts для этого telegram_message_id пуст.
    message = _fake_message(
        chat_id,
        941014,
        "согласен",
        reply_to_message_id=800_100,
        reply_to_from_user_id=target_id,
        sent_message_id=800_101,
    )

    await daily_twin_handlers.daily_twin_reaction(message, session)

    message.answer.assert_awaited_once()
    text = message.answer.await_args.args[0]
    assert text == "норм чё"


@pytest.mark.asyncio
async def test_reacts_to_text_mention_of_todays_twin(session, monkeypatch):
    monkeypatch.setattr(twin_service.ai_client, "stream", _fake_stream)

    chat_id = -100941015
    target_id = 941015
    await _ensure_user(session, target_id, "Дима")
    await _set_opt_in(session, chat_id, target_id, "active")
    await session.commit()

    text = "эй Дима ты как обычно"
    message = _fake_message(
        chat_id,
        941016,
        text,
        entities=[_entity("text_mention", 3, len("Дима"), user=SimpleNamespace(id=target_id))],
        sent_message_id=800_201,
    )

    await daily_twin_handlers.daily_twin_reaction(message, session)

    message.answer.assert_awaited_once()
    assert message.answer.await_args.args[0] == "норм чё"


@pytest.mark.asyncio
async def test_reacts_to_plain_username_mention_of_todays_twin(session, monkeypatch):
    monkeypatch.setattr(twin_service.ai_client, "stream", _fake_stream)

    chat_id = -100941017
    target_id = 941017
    await _ensure_user(session, target_id, "Дима", username="dima_the_best")
    await _set_opt_in(session, chat_id, target_id, "active")
    await session.commit()

    text = "@dima_the_best го в казино"
    message = _fake_message(
        chat_id,
        941018,
        text,
        entities=[_entity("mention", 0, len("@dima_the_best"))],
        sent_message_id=800_301,
    )

    await daily_twin_handlers.daily_twin_reaction(message, session)

    message.answer.assert_awaited_once()
    assert message.answer.await_args.args[0] == "норм чё"


@pytest.mark.asyncio
async def test_reaction_skips_at_max_posts_cap(session):
    """Тот же суточный потолок `settings.daily_twin_max_posts`, что и у
    проактивного тика (`daily_twin_service.maybe_post_proactively`) — до
    этого теста реактивная ветка не имела потолка вообще и отвечала на
    КАЖДЫЙ матчащий апдейт без остановки (живой инцидент 2026-07-29,
    воспринималось как спам, когда сегодняшняя персона оказалась активным
    участником треда). `count_todays_posts` считает посты обоих путей
    (одна таблица `daily_twin_posts`), поэтому проверка не требует
    отдельного счётчика — переиспользует уже занятый сегодня бюджет.
    Гейт срабатывает ДО любого AI-вызова — ai_client.stream не патчится."""
    from bot.config import settings

    chat_id = -100941024
    target_id = 941024
    await _ensure_user(session, target_id, "Дима")
    await _set_opt_in(session, chat_id, target_id, "active")
    for i in range(settings.daily_twin_max_posts):
        await daily_twin_service.record_post(session, chat_id, 800_500 + i, target_id)
    await session.commit()

    message = _fake_message(
        chat_id,
        941025,
        "го ещё раз",
        reply_to_message_id=800_500,
        sent_message_id=800_600,
    )

    await daily_twin_handlers.daily_twin_reaction(message, session)

    message.answer.assert_not_awaited()


@pytest.mark.asyncio
async def test_does_not_react_to_self_mention(session, monkeypatch):
    """Сегодняшняя персона сама упоминает/комментирует себя — не отвечает
    своим же голосом самой себе."""
    monkeypatch.setattr(twin_service.ai_client, "stream", _fake_stream)

    chat_id = -100941019
    target_id = 941019
    await _ensure_user(session, target_id, "Дима", username="dima_the_best")
    await _set_opt_in(session, chat_id, target_id, "active")
    await session.commit()

    text = "@dima_the_best это я сам про себя"
    message = _fake_message(
        chat_id,
        target_id,  # автор сообщения == сегодняшняя персона
        text,
        entities=[_entity("mention", 0, len("@dima_the_best"))],
    )

    with pytest.raises(SkipHandler):
        await daily_twin_handlers.daily_twin_reaction(message, session)

    message.answer.assert_not_awaited()


@pytest.mark.asyncio
async def test_own_bot_post_reply_takes_priority_over_mention(session, monkeypatch):
    """Реплай на пост бота ПРИОРИТЕТНЕЕ мэтча по упоминанию — если сообщение
    одновременно и реплай на daily_twin_posts, и содержит @упоминание
    какого-то другого human'а, отвечает персона ИЗ ЖУРНАЛА поста, не персона
    из упоминания (не тратим лишний DB-запрос get_todays_twin, см. хендлер)."""
    monkeypatch.setattr(twin_service.ai_client, "stream", _fake_stream)

    chat_id = -100941021
    post_target_id = 941021
    mentioned_id = 941022
    twin_post_message_id = 800_401
    await _ensure_user(session, post_target_id, "Дима")
    await _ensure_user(session, mentioned_id, "Другой", username="other_guy")
    await _set_opt_in(session, chat_id, post_target_id, "active")
    await daily_twin_service.record_post(session, chat_id, twin_post_message_id, post_target_id)
    await session.commit()

    message = _fake_message(
        chat_id,
        941023,
        "@other_guy глянь чё пишет",
        reply_to_message_id=twin_post_message_id,
        entities=[_entity("mention", 0, len("@other_guy"))],
        sent_message_id=800_402,
    )

    await daily_twin_handlers.daily_twin_reaction(message, session)

    message.answer.assert_awaited_once()
    assert message.answer.await_args.args[0] == "норм чё"
