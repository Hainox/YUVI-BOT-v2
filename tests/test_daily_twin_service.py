"""Тесты дневного двойника (TWIN-03, bot/services/daily_twin_service.py)
против живого Postgres (фикстура `session` — транзакция-на-тест).

Доказывают:
- Персона дня выбирается ТОЛЬКО из активных согласий `/twin`
  (twin_opt_ins.status == 'active') и идемпотентна на MSK-день (поверх
  daily_pick_service, kind='daily_twin').
- Журнал постов (DailyTwinPost) корректно связывает telegram_message_id с
  target_user_id (для реактивного хендлера) и считает посты за конкретный день.
- `maybe_post_proactively` (тестируемое ядро тика) уважает: рабочее окно
  часов, потолок постов в день, вероятностный бросок, деградацию AI
  (не постит заглушку как "реплику человека"), и НЕ глотает
  `TwinConsentError`, если согласие отозвано в середине дня (вызывающий,
  `_job`, обязан сам отличить это от прочих ошибок).

`ai_client.stream` мокается той же формой async-generator'а, что и
test_twin_service.py (реального похода к OpenCode Go нет)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from bot.services import daily_twin_service
from bot.services import twin_service
from common.models.user import User


async def _ensure_user(session, user_id: int, first_name: str = "Тест") -> None:
    session.add(User(id=user_id, first_name=first_name))
    await session.flush()


async def _set_opt_in(session, chat_id: int, user_id: int, status: str) -> None:
    """Upsert (не raw insert) — тест `..._revoked_mid_day` меняет статус
    ДВАЖДЫ для одного и того же (chat_id, user_id), raw insert упал бы на
    UNIQUE(chat_id, user_id). Тонкая обёртка над реальным
    twin_service.set_opt_in (сам не коммитит)."""
    await twin_service.set_opt_in(session, chat_id, user_id, status)


async def _fake_stream(messages: list[dict], model: str, max_tokens: int) -> AsyncIterator[str]:
    yield "тестовая реплика"


async def _raising_stream(messages: list[dict], model: str, max_tokens: int) -> AsyncIterator[str]:
    raise RuntimeError("Модель вернула только reasoning без ответа")
    yield  # pragma: no cover - unreachable, делает функцию async-генератором


class _FixedDatetime:
    """Подмена module-level `datetime` в daily_twin_service — единственное
    использование в модуле — `datetime.now(tz)` внутри maybe_post_proactively."""

    _fixed_hour: int = 13

    @classmethod
    def now(cls, tz=None):
        return datetime(2026, 1, 1, cls._fixed_hour, 0, tzinfo=tz)


def _fake_bot() -> SimpleNamespace:
    return SimpleNamespace(
        send_message=AsyncMock(return_value=SimpleNamespace(message_id=555_001))
    )


# --- get_todays_twin: только активные согласия, идемпотентно на MSK-день ----


@pytest.mark.asyncio
async def test_get_todays_twin_returns_none_without_candidates(session):
    chat_id = -100940001
    assert await daily_twin_service.get_todays_twin(session, chat_id) is None


@pytest.mark.asyncio
async def test_get_todays_twin_picks_only_active_candidate(session):
    chat_id = -100940002
    active_id, paused_id, declined_id = 940002, 940003, 940004
    await _ensure_user(session, active_id, "Активный")
    await _ensure_user(session, paused_id, "На-паузе")
    await _ensure_user(session, declined_id, "Отказался")
    await _set_opt_in(session, chat_id, active_id, "active")
    await _set_opt_in(session, chat_id, paused_id, "paused")
    await _set_opt_in(session, chat_id, declined_id, "declined")
    await session.commit()

    twin = await daily_twin_service.get_todays_twin(session, chat_id)

    assert twin == (active_id, "Активный", None)


@pytest.mark.asyncio
async def test_get_todays_twin_idempotent_same_day(session):
    chat_id = -100940005
    user_id = 940005
    await _ensure_user(session, user_id, "Один")
    await _set_opt_in(session, chat_id, user_id, "active")
    await session.commit()

    first = await daily_twin_service.get_todays_twin(session, chat_id)
    second = await daily_twin_service.get_todays_twin(session, chat_id)

    assert first == second == (user_id, "Один", None)


# --- журнал постов: count/record/find --------------------------------------


@pytest.mark.asyncio
async def test_count_todays_posts_scoped_to_chat_and_day(session):
    chat_id = -100940006
    other_chat_id = -100940007
    user_id = 940006
    await _ensure_user(session, user_id, "Тест")
    await session.commit()

    assert await daily_twin_service.count_todays_posts(session, chat_id) == 0

    await daily_twin_service.record_post(session, chat_id, 111, user_id)
    await daily_twin_service.record_post(session, chat_id, 112, user_id)
    await daily_twin_service.record_post(session, other_chat_id, 113, user_id)
    await session.commit()

    assert await daily_twin_service.count_todays_posts(session, chat_id) == 2
    assert await daily_twin_service.count_todays_posts(session, other_chat_id) == 1


@pytest.mark.asyncio
async def test_record_post_and_find_target_by_post_roundtrip(session):
    chat_id = -100940008
    user_id = 940008
    await _ensure_user(session, user_id, "Тест")
    await daily_twin_service.record_post(session, chat_id, 222_001, user_id)
    await session.commit()

    found = await daily_twin_service.find_target_by_post(session, chat_id, 222_001)
    assert found == user_id


@pytest.mark.asyncio
async def test_find_target_by_post_returns_none_for_unknown_message(session):
    chat_id = -100940009
    assert await daily_twin_service.find_target_by_post(session, chat_id, 999_999) is None


# --- _in_posting_window: чистая функция, без БД ------------------------------


def test_in_posting_window_boundaries():
    tz = None
    assert daily_twin_service._in_posting_window(datetime(2026, 1, 1, 8, 59, tzinfo=tz)) is False
    assert daily_twin_service._in_posting_window(datetime(2026, 1, 1, 9, 0, tzinfo=tz)) is True
    assert daily_twin_service._in_posting_window(datetime(2026, 1, 1, 13, 0, tzinfo=tz)) is True
    assert daily_twin_service._in_posting_window(datetime(2026, 1, 1, 22, 59, tzinfo=tz)) is True
    assert daily_twin_service._in_posting_window(datetime(2026, 1, 1, 23, 0, tzinfo=tz)) is False
    assert daily_twin_service._in_posting_window(datetime(2026, 1, 1, 0, 0, tzinfo=tz)) is False


# --- maybe_post_proactively: полное ядро тика --------------------------------


@pytest.mark.asyncio
async def test_maybe_post_proactively_skips_outside_window(session, monkeypatch):
    _FixedDatetime._fixed_hour = 6  # до окна 9:00-23:00
    monkeypatch.setattr(daily_twin_service, "datetime", _FixedDatetime)
    chat_id = -100940010
    user_id = 940010
    await _ensure_user(session, user_id, "Тест")
    await _set_opt_in(session, chat_id, user_id, "active")
    await session.commit()
    bot = _fake_bot()

    posted = await daily_twin_service.maybe_post_proactively(session, chat_id, bot)

    assert posted is False
    bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_maybe_post_proactively_skips_without_candidates(session, monkeypatch):
    _FixedDatetime._fixed_hour = 13
    monkeypatch.setattr(daily_twin_service, "datetime", _FixedDatetime)
    chat_id = -100940011
    bot = _fake_bot()

    posted = await daily_twin_service.maybe_post_proactively(session, chat_id, bot)

    assert posted is False
    bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_maybe_post_proactively_skips_at_max_posts_cap(session, monkeypatch):
    _FixedDatetime._fixed_hour = 13
    monkeypatch.setattr(daily_twin_service, "datetime", _FixedDatetime)
    monkeypatch.setattr(daily_twin_service, "_tick_probability", lambda: 1.0)
    chat_id = -100940012
    user_id = 940012
    await _ensure_user(session, user_id, "Тест")
    await _set_opt_in(session, chat_id, user_id, "active")
    from bot.config import settings

    for i in range(settings.daily_twin_max_posts):
        await daily_twin_service.record_post(session, chat_id, 300_000 + i, user_id)
    await session.commit()
    bot = _fake_bot()

    posted = await daily_twin_service.maybe_post_proactively(session, chat_id, bot)

    assert posted is False
    bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_maybe_post_proactively_skips_on_probability_miss(session, monkeypatch):
    _FixedDatetime._fixed_hour = 13
    monkeypatch.setattr(daily_twin_service, "datetime", _FixedDatetime)
    monkeypatch.setattr(daily_twin_service, "_tick_probability", lambda: 0.0)
    chat_id = -100940013
    user_id = 940013
    await _ensure_user(session, user_id, "Тест")
    await _set_opt_in(session, chat_id, user_id, "active")
    await session.commit()
    bot = _fake_bot()

    posted = await daily_twin_service.maybe_post_proactively(session, chat_id, bot)

    assert posted is False
    bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_maybe_post_proactively_posts_and_records(session, monkeypatch):
    _FixedDatetime._fixed_hour = 13
    monkeypatch.setattr(daily_twin_service, "datetime", _FixedDatetime)
    monkeypatch.setattr(daily_twin_service, "_tick_probability", lambda: 1.0)
    monkeypatch.setattr(twin_service.ai_client, "stream", _fake_stream)
    chat_id = -100940014
    user_id = 940014
    await _ensure_user(session, user_id, "Тест")
    await _set_opt_in(session, chat_id, user_id, "active")
    await session.commit()
    bot = _fake_bot()

    posted = await daily_twin_service.maybe_post_proactively(session, chat_id, bot)

    assert posted is True
    bot.send_message.assert_awaited_once()
    sent_text = bot.send_message.await_args.args[1]
    assert "тестовая реплика" in sent_text
    assert "Двойник дня" not in sent_text

    found = await daily_twin_service.find_target_by_post(session, chat_id, 555_001)
    assert found == user_id


@pytest.mark.asyncio
async def test_maybe_post_proactively_skips_fallback_text(session, monkeypatch):
    _FixedDatetime._fixed_hour = 13
    monkeypatch.setattr(daily_twin_service, "datetime", _FixedDatetime)
    monkeypatch.setattr(daily_twin_service, "_tick_probability", lambda: 1.0)
    monkeypatch.setattr(twin_service.ai_client, "stream", _raising_stream)
    chat_id = -100940015
    user_id = 940015
    await _ensure_user(session, user_id, "Тест")
    await _set_opt_in(session, chat_id, user_id, "active")
    await session.commit()
    bot = _fake_bot()

    posted = await daily_twin_service.maybe_post_proactively(session, chat_id, bot)

    assert posted is False
    bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_maybe_post_proactively_propagates_consent_error_if_revoked_mid_day(session, monkeypatch):
    """Пик персоны дня уже зафиксирован (daily_picks) — но согласие человека
    ОТОЗВАНО с момента выбора. maybe_post_proactively НЕ должна проглатывать
    TwinConsentError изнутри build_twin_reply — это отдаётся вызывающему
    (_job), чтобы отличить "штатный скип по паузе" от прочих ошибок."""
    _FixedDatetime._fixed_hour = 13
    monkeypatch.setattr(daily_twin_service, "datetime", _FixedDatetime)
    monkeypatch.setattr(daily_twin_service, "_tick_probability", lambda: 1.0)
    monkeypatch.setattr(twin_service.ai_client, "stream", _fake_stream)
    chat_id = -100940016
    user_id = 940016
    await _ensure_user(session, user_id, "Тест")
    await _set_opt_in(session, chat_id, user_id, "active")
    await session.commit()
    bot = _fake_bot()

    # Первый тик фиксирует пик персоны дня и реально постит.
    posted = await daily_twin_service.maybe_post_proactively(session, chat_id, bot)
    assert posted is True

    # Согласие отзывается в середине дня.
    await _set_opt_in(session, chat_id, user_id, "paused")
    await session.commit()

    with pytest.raises(twin_service.TwinConsentError):
        await daily_twin_service.maybe_post_proactively(session, chat_id, bot)
