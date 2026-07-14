"""Интеграционные тесты stats_service против живого Postgres.

Доказывает D-06 (all-time по умолчанию, учёт периода `days`) и корректность
всех пяти функций чтения агрегатов STATS-01, а также STATS-02 (старые
команды мапятся на те же функции stats_service, что и новые, без
дублирующей SQL-логики).
"""

from __future__ import annotations

import html
from datetime import date
from datetime import datetime
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest

import bot.handlers.stats as stats_handlers
from bot.services import stats_service
from common.models.daily_stat import DailyStat
from common.models.user import User
from common.models.word_frequency import WordFrequency

MSK = ZoneInfo("Europe/Moscow")


async def _seed_daily_stats(session, chat_id: int, user_id: int, rows: list[tuple[date, int]]) -> None:
    session.add(User(id=user_id, first_name="Тест"))
    await session.flush()
    for stat_date, count in rows:
        session.add(
            DailyStat(chat_id=chat_id, user_id=user_id, stat_date=stat_date, message_count=count)
        )
    await session.flush()


async def _seed_word_frequency(session, chat_id: int, user_id: int, first_name: str, words: dict[str, int]) -> None:
    session.add(User(id=user_id, first_name=first_name))
    await session.flush()
    for word, count in words.items():
        session.add(WordFrequency(chat_id=chat_id, user_id=user_id, word=word, count=count))
    await session.flush()


def _fake_message(chat_id: int, user_id: int, first_name: str, text: str):
    """Минимальный aiogram-подобный Message для теста тонких хендлеров:
    только атрибуты, которые реально читают хендлеры stats.py, плюс
    AsyncMock на answer() вместо реального похода в Telegram.
    """
    return SimpleNamespace(
        chat=SimpleNamespace(id=chat_id),
        from_user=SimpleNamespace(id=user_id, first_name=first_name),
        text=text,
        answer=AsyncMock(),
    )


@pytest.mark.asyncio
async def test_get_chat_message_count_all_time_sums_everything(session):
    chat_id = -100987654321
    user_id = 700000001
    today = datetime.now(MSK).date()

    await _seed_daily_stats(
        session,
        chat_id,
        user_id,
        [
            (today, 5),
            (today - timedelta(days=10), 3),
            (today - timedelta(days=100), 7),
        ],
    )

    total = await stats_service.get_chat_message_count(session, chat_id, days=None)

    assert total == 15


@pytest.mark.asyncio
async def test_get_chat_message_count_respects_period_argument(session):
    chat_id = -100987654322
    user_id = 700000002
    today = datetime.now(MSK).date()

    await _seed_daily_stats(
        session,
        chat_id,
        user_id,
        [
            (today, 4),
            (today - timedelta(days=5), 6),
            (today - timedelta(days=40), 20),
        ],
    )

    total_last_30 = await stats_service.get_chat_message_count(session, chat_id, days=30)

    assert total_last_30 == 10


@pytest.mark.asyncio
async def test_get_chat_message_count_returns_zero_for_unknown_chat(session):
    total = await stats_service.get_chat_message_count(session, chat_id=-1, days=None)

    assert total == 0


# --- get_user_stats -------------------------------------------------------


@pytest.mark.asyncio
async def test_get_user_stats_returns_totals_and_date_range(session):
    chat_id = -100900000001
    user_id = 700000010
    today = datetime.now(MSK).date()

    await _seed_daily_stats(
        session,
        chat_id,
        user_id,
        [
            (today, 3),
            (today - timedelta(days=1), 2),
            (today - timedelta(days=10), 5),
        ],
    )

    stats = await stats_service.get_user_stats(session, chat_id, user_id, days=None)

    assert stats["total_messages"] == 10
    assert stats["active_days"] == 3
    assert stats["first_active_date"] == today - timedelta(days=10)
    assert stats["last_active_date"] == today


@pytest.mark.asyncio
async def test_get_user_stats_respects_period_argument(session):
    chat_id = -100900000002
    user_id = 700000011
    today = datetime.now(MSK).date()

    await _seed_daily_stats(
        session,
        chat_id,
        user_id,
        [
            (today, 4),
            (today - timedelta(days=2), 6),
            (today - timedelta(days=50), 100),
        ],
    )

    stats = await stats_service.get_user_stats(session, chat_id, user_id, days=7)

    assert stats["total_messages"] == 10
    assert stats["active_days"] == 2


# --- get_top_participants --------------------------------------------------


@pytest.mark.asyncio
async def test_get_top_participants_orders_descending(session):
    chat_id = -100900000003
    today = datetime.now(MSK).date()

    await _seed_daily_stats(session, chat_id, 700000020, [(today, 5)])
    await _seed_daily_stats(session, chat_id, 700000021, [(today, 20)])
    await _seed_daily_stats(session, chat_id, 700000022, [(today, 10)])

    top = await stats_service.get_top_participants(session, chat_id, days=None, limit=10)

    assert [row["user_id"] for row in top] == [700000021, 700000022, 700000020]
    assert [row["message_count"] for row in top] == [20, 10, 5]
    # Имя резолвится из users, не сырой telegram id.
    assert top[0]["first_name"] == "Тест"


# --- get_streak --------------------------------------------------------


@pytest.mark.asyncio
async def test_get_streak_counts_consecutive_days(session):
    chat_id = -100900000004
    user_id = 700000030
    today = datetime.now(MSK).date()

    await _seed_daily_stats(
        session,
        chat_id,
        user_id,
        [
            (today, 1),
            (today - timedelta(days=1), 1),
            (today - timedelta(days=2), 1),
        ],
    )

    streak = await stats_service.get_streak(session, chat_id, user_id)

    assert streak == 3


@pytest.mark.asyncio
async def test_get_streak_breaks_on_gap(session):
    chat_id = -100900000005
    user_id = 700000031
    today = datetime.now(MSK).date()

    await _seed_daily_stats(
        session,
        chat_id,
        user_id,
        [
            (today, 1),
            (today - timedelta(days=1), 1),
            # разрыв на today-2
            (today - timedelta(days=3), 1),
        ],
    )

    streak = await stats_service.get_streak(session, chat_id, user_id)

    assert streak == 2


@pytest.mark.asyncio
async def test_get_streak_returns_zero_when_no_activity(session):
    streak = await stats_service.get_streak(session, chat_id=-1, user_id=-1)

    assert streak == 0


# --- get_peak_day --------------------------------------------------------


@pytest.mark.asyncio
async def test_get_peak_day_returns_highest_day(session):
    chat_id = -100900000006
    today = datetime.now(MSK).date()

    await _seed_daily_stats(session, chat_id, 700000040, [(today, 3), (today - timedelta(days=1), 50)])
    await _seed_daily_stats(session, chat_id, 700000041, [(today, 2), (today - timedelta(days=1), 1)])

    peak = await stats_service.get_peak_day(session, chat_id, days=None)

    assert peak == (today - timedelta(days=1), 51)


@pytest.mark.asyncio
async def test_get_peak_day_respects_period_argument(session):
    chat_id = -100900000007
    today = datetime.now(MSK).date()

    await _seed_daily_stats(
        session,
        chat_id,
        700000050,
        [(today, 5), (today - timedelta(days=40), 999)],
    )

    peak = await stats_service.get_peak_day(session, chat_id, days=7)

    assert peak == (today, 5)


@pytest.mark.asyncio
async def test_get_peak_day_returns_none_for_unknown_chat(session):
    peak = await stats_service.get_peak_day(session, chat_id=-1, days=None)

    assert peak is None


# --- get_top_words --------------------------------------------------------


@pytest.mark.asyncio
async def test_get_top_words_orders_by_count(session):
    chat_id = -100900000008

    await _seed_word_frequency(session, chat_id, 700000060, "Тест1", {"привет": 3, "как": 1})
    await _seed_word_frequency(session, chat_id, 700000061, "Тест2", {"привет": 5, "дела": 2})

    top = await stats_service.get_top_words(session, chat_id, days=None, limit=10)

    assert top[0] == {"word": "привет", "count": 8}
    words = [row["word"] for row in top]
    assert "как" in words and "дела" in words


# --- STATS-02: старые команды без дублирующей SQL-логики -----------------


def test_stats02_stats_alias_maps_to_same_handler_as_mystats():
    """`/stats` зарегистрирован как альтернативное имя ТОЙ ЖЕ Command-фильтрации,
    что и `/mystats` — не отдельная функция с собственным SQL."""
    commands = _handler_commands(stats_handlers.mystats_command)
    assert commands == {"mystats", "stats"}


def test_stats02_top_alias_maps_to_same_handler_as_who():
    commands = _handler_commands(stats_handlers.who_command)
    assert commands == {"who", "top"}


def test_stats02_activity_alias_maps_to_same_handler_as_peakday():
    commands = _handler_commands(stats_handlers.peakday_command)
    assert commands == {"peakday", "activity"}


def _handler_commands(callback) -> set[str]:
    for handler in stats_handlers.router.message.handlers:
        if handler.callback is callback:
            for filt in handler.filters:
                cmd = getattr(filt.callback, "commands", None)
                if cmd is not None:
                    return set(cmd)
    raise AssertionError(f"handler not registered: {callback}")


@pytest.mark.asyncio
async def test_stats02_words_command_uses_same_service_function_as_chatstats(session, monkeypatch):
    """/words не заводит отдельный SQL-запрос — вызывает тот же
    stats_service.get_top_words, что /chatstats использует внутри себя."""
    chat_id = -100900000009
    await _seed_word_frequency(session, chat_id, 700000070, "Тест3", {"слово": 7})

    calls: list[tuple] = []
    original = stats_service.get_top_words

    async def _tracking_get_top_words(*args, **kwargs):
        calls.append((args, kwargs))
        return await original(*args, **kwargs)

    monkeypatch.setattr(stats_service, "get_top_words", _tracking_get_top_words)

    message = _fake_message(chat_id, 700000070, "Тест3", "/words")
    await stats_handlers.words_command(message, session)

    assert len(calls) == 1
    message.answer.assert_awaited_once()
    sent_text = message.answer.await_args.args[0]
    assert html.escape("слово") in sent_text
    assert "7" in sent_text


@pytest.mark.asyncio
async def test_mystats_and_stats_alias_produce_identical_output(session):
    """Так как /mystats и /stats — один и тот же обработчик (см. тест выше
    на уровне Command-фильтра), прямой вызов подтверждает идентичный вывод
    при одинаковых входных данных (STATS-02 equivalence)."""
    chat_id = -100900000010
    user_id = 700000080
    today = datetime.now(MSK).date()
    await _seed_daily_stats(session, chat_id, user_id, [(today, 4)])

    message_a = _fake_message(chat_id, user_id, "Тест4", "/mystats")
    message_b = _fake_message(chat_id, user_id, "Тест4", "/stats")

    await stats_handlers.mystats_command(message_a, session)
    await stats_handlers.mystats_command(message_b, session)

    text_a = message_a.answer.await_args.args[0]
    text_b = message_b.answer.await_args.args[0]
    assert text_a == text_b
