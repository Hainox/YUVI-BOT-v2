"""Тесты digest_service (AI-02, формат "Дайджест чата" 2026-07-27).

Два уровня:
- Чистые unit-тесты detect_bursts/pick_burst_quotes/форматирования — точные
  сценарии на синтетических данных (без БД), потому что это НОВАЯ, нигде
  раньше не существовавшая логика (детектор "горячих окон").
- Интеграционный smoke-тест build_digest/build_manual_digest против живого
  Postgres с реальными Message-строками — доказывает, что вся сборка (шапка/
  ВСПЛЕСКИ/ЦИТАТЫ/ВАЙБ/Пересказ/Активные) не падает и попадает в текст, не
  проверяя точные числа всплесков (это уже покрыто unit-тестами detect_bursts).

D-03/D-12 (порог автодайджеста): build_digest не должен звать ai_client.stream
ни разу, если активность дня ниже settings.digest_min_messages.
"""

from __future__ import annotations

from datetime import date
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from zoneinfo import ZoneInfo

import pytest

from bot.config import settings
from bot.services import digest_service
from common.models.daily_stat import DailyStat
from common.models.message import Message
from common.models.user import User

MSK = ZoneInfo("Europe/Moscow")


async def _ensure_user(session, user_id: int, first_name: str = "Тест", username: str | None = None) -> None:
    session.add(User(id=user_id, first_name=first_name, username=username))
    await session.flush()


async def _seed_daily_stat(session, chat_id: int, user_id: int, stat_date: date, count: int) -> None:
    session.add(DailyStat(chat_id=chat_id, user_id=user_id, stat_date=stat_date, message_count=count))
    await session.flush()


def _today_msk() -> date:
    return datetime.now(MSK).date()


_msg_seq = iter(range(1, 10_000_000))


async def _seed_message(session, chat_id: int, user_id: int, created_at_utc: datetime, text: str) -> None:
    """created_at хранится naive UTC (см. common/models/message.py) — снимаем
    tzinfo, если вызывающий передал tz-aware datetime.now(timezone.utc)."""
    if created_at_utc.tzinfo is not None:
        created_at_utc = created_at_utc.astimezone(timezone.utc).replace(tzinfo=None)
    session.add(
        Message(
            chat_id=chat_id,
            user_id=user_id,
            telegram_message_id=next(_msg_seq),
            text=text,
            created_at=created_at_utc,
        )
    )
    await session.flush()


def _msg(created_at: datetime, text: str = "текст", user_id: int = 1, first_name: str = "Аня", username=None) -> dict:
    return {"user_id": user_id, "first_name": first_name, "username": username, "text": text, "created_at": created_at}


# --- count_day_messages / count_period_messages -------------------------------


@pytest.mark.asyncio
async def test_count_day_messages_counts_only_exact_date(session):
    chat_id = -100920000001
    user_id = 700200001
    await _ensure_user(session, user_id)
    today = _today_msk()
    yesterday = today - timedelta(days=1)

    await _seed_daily_stat(session, chat_id, user_id, today, 7)
    await _seed_daily_stat(session, chat_id, user_id, yesterday, 3)

    count = await digest_service.count_day_messages(session, chat_id, today)

    assert count == 7


@pytest.mark.asyncio
async def test_count_day_messages_zero_for_unknown_chat(session):
    count = await digest_service.count_day_messages(session, chat_id=-1, day=_today_msk())
    assert count == 0


@pytest.mark.asyncio
async def test_count_period_messages_sums_range_and_excludes_outside(session):
    chat_id = -100920000010
    user_id = 700200010
    await _ensure_user(session, user_id)
    today = _today_msk()
    await _seed_daily_stat(session, chat_id, user_id, today, 5)
    await _seed_daily_stat(session, chat_id, user_id, today - timedelta(days=1), 3)
    await _seed_daily_stat(session, chat_id, user_id, today - timedelta(days=5), 100)  # вне диапазона

    total = await digest_service.count_period_messages(session, chat_id, today - timedelta(days=1), today)

    assert total == 8


# --- detect_bursts (pure function, синтетические данные) ----------------------


def test_detect_bursts_empty_input_returns_empty():
    assert digest_service.detect_bursts([]) == []


def test_detect_bursts_ignores_quiet_period_below_threshold():
    base = datetime(2026, 5, 1, 12, 0, 0)
    messages = [_msg(base + timedelta(minutes=i)) for i in range(5)]  # 5 < min_count по умолчанию

    bursts = digest_service.detect_bursts(messages, min_count=15)

    assert bursts == []


def test_detect_bursts_finds_single_dense_window():
    base = datetime(2026, 5, 1, 12, 0, 0)
    # 20 сообщений за 10 минут -> плотное окно, остальное — тишина.
    dense = [_msg(base + timedelta(seconds=30 * i)) for i in range(20)]
    sparse = [_msg(base + timedelta(hours=5 + i)) for i in range(3)]
    messages = dense + sparse

    bursts = digest_service.detect_bursts(
        messages, window_minutes=40, bucket_minutes=5, top_k=5, min_count=15
    )

    assert len(bursts) == 1
    assert bursts[0].count == 20
    assert bursts[0].start <= dense[0]["created_at"]
    assert bursts[0].end >= dense[-1]["created_at"]


def test_detect_bursts_selects_non_overlapping_windows_in_chronological_order():
    base = datetime(2026, 5, 1, 8, 0, 0)
    burst_a = [_msg(base + timedelta(seconds=20 * i)) for i in range(20)]  # раньше по времени
    burst_b = [_msg(base + timedelta(hours=6, seconds=20 * i)) for i in range(30)]  # позже, плотнее

    bursts = digest_service.detect_bursts(
        burst_a + burst_b, window_minutes=40, bucket_minutes=5, top_k=5, min_count=15
    )

    assert len(bursts) == 2
    # Хронологический порядок в результате — НЕ по убыванию объёма (burst_b плотнее, но позже).
    assert bursts[0].start < bursts[1].start
    assert bursts[1].count == 30


def test_detect_bursts_respects_top_k():
    base = datetime(2026, 5, 1, 0, 0, 0)
    # 4 отдельных плотных окна, разнесённых на 3 часа друг от друга.
    messages = []
    for slot in range(4):
        slot_base = base + timedelta(hours=3 * slot)
        messages.extend(_msg(slot_base + timedelta(seconds=20 * i)) for i in range(20))

    bursts = digest_service.detect_bursts(
        messages, window_minutes=40, bucket_minutes=5, top_k=2, min_count=15
    )

    assert len(bursts) == 2


# --- pick_burst_quotes ---------------------------------------------------------


def test_pick_burst_quotes_picks_longest_message_per_burst():
    burst = digest_service.Burst(
        start=datetime(2026, 5, 1, 12, 0),
        end=datetime(2026, 5, 1, 12, 40),
        count=3,
        messages=[
            _msg(datetime(2026, 5, 1, 12, 1), text="коротко", username="short"),
            _msg(datetime(2026, 5, 1, 12, 2), text="а" * 100, username="long"),
            _msg(datetime(2026, 5, 1, 12, 3), text="средне так себе", username="mid"),
        ],
    )

    quotes = digest_service.pick_burst_quotes([burst])

    assert len(quotes) == 1
    assert quotes[0]["username"] == "long"
    assert quotes[0]["burst_index"] == 1


def test_pick_burst_quotes_skips_burst_without_long_enough_message():
    burst = digest_service.Burst(
        start=datetime(2026, 5, 1, 12, 0),
        end=datetime(2026, 5, 1, 12, 40),
        count=2,
        messages=[_msg(datetime(2026, 5, 1, 12, 1), text="коротко"), _msg(datetime(2026, 5, 1, 12, 2), text="тоже")],
    )

    assert digest_service.pick_burst_quotes([burst], min_length=40) == []


# --- форматирование (чистые функции) -------------------------------------------


def test_top_authors_prefers_username_and_ranks_by_count():
    messages = (
        [_msg(datetime.now(), user_id=1, username="atonyan") for _ in range(5)]
        + [_msg(datetime.now(), user_id=2, first_name="БезЮзернейма") for _ in range(2)]
    )

    ranked = digest_service._top_authors(messages, limit=5)

    assert ranked[0] == ("@atonyan", 5)
    assert ranked[1] == ("БезЮзернейма", 2)


def test_active_participants_line_deduplicates_by_user():
    messages = [_msg(datetime.now(), user_id=1, username="a") for _ in range(3)] + [
        _msg(datetime.now(), user_id=2, username="b")
    ]

    line = digest_service._active_participants_line(messages)

    assert line == "Активные: @a, @b"


# --- build_digest: порог D-03/D-12 (не должен звать LLM) -----------------------


@pytest.mark.asyncio
async def test_skips_low_activity_day_without_calling_llm(session, monkeypatch):
    chat_id = -100920000002
    user_id = 700200002
    await _ensure_user(session, user_id)
    today = _today_msk()
    await _seed_daily_stat(session, chat_id, user_id, today, settings.digest_min_messages - 1)

    def _boom(*args, **kwargs):
        raise AssertionError("build_digest не должен звать ai_client.stream при низкой активности")

    monkeypatch.setattr(digest_service.ai_client, "stream", _boom)

    result = await digest_service.build_digest(session, chat_id)

    assert result is None


@pytest.mark.asyncio
async def test_returns_none_for_zero_activity_day(session):
    chat_id = -100920000003
    result = await digest_service.build_digest(session, chat_id)
    assert result is None


# --- build_digest / build_manual_digest: сборка end-to-end (LLM замокан) ------


async def _fake_stream(messages, model, max_tokens):
    system_content = messages[0]["content"]
    if "горячих окон" in system_content:
        for i in range(1, 10):
            yield f"{i}: движуха в чате, все обсуждают всякое.\n"
    elif "АТМОСФЕРА" in system_content:
        yield "ВАЙБ\nВайб бодрый и слегка токсичный, все как обычно.\nАТМОСФЕРА: жиза и подколы"
    else:
        yield '1. **Тема дня** — обсуждали всякое, "цитата" от участника.'


@pytest.mark.asyncio
async def test_build_digest_assembles_all_sections(session, monkeypatch):
    chat_id = -100920000004
    user_id = 700200004
    await _ensure_user(session, user_id, first_name="Аня", username="anya")
    today = _today_msk()
    # 25 текстовых сообщений в 10-минутном окне -> и порог count_day_messages,
    # и порог detect_bursts.min_count пройдены одним и тем же плотным периодом.
    await _seed_daily_stat(session, chat_id, user_id, today, 25)
    # Якорим от MSK-даты `today` (не от datetime.now(timezone.utc)) — в окне
    # 21:00-00:00 UTC (00:00-03:00 МСК) эти два "сегодня" расходятся на день,
    # тест ловил Message.created_at ВНЕ границ [today 00:00 МСК, today+1)
    # (digest_service._msk_day_bounds_utc), которые daily_stat уже засеян под
    # другую дату — 13:00 МСК гарантированно внутри суток `today` всегда.
    base_utc = (
        datetime.combine(today, datetime.min.time(), tzinfo=MSK)
        .replace(hour=13)
        .astimezone(timezone.utc)
        .replace(tzinfo=None)
    )
    for i in range(25):
        await _seed_message(
            session, chat_id, user_id, base_utc + timedelta(seconds=20 * i), f"сообщение номер {i} про котиков"
        )

    monkeypatch.setattr(digest_service.ai_client, "stream", _fake_stream)

    result = await digest_service.build_digest(session, chat_id)

    assert result is not None
    assert "Дайджест чата" in result
    assert "Всего сообщений: 25" in result
    assert "Топ авторов: @anya" in result
    assert "Горячие окна: B1" in result
    assert "ВСПЛЕСКИ" in result
    assert "[BURST 1:" in result
    assert "ЦИТАТЫ" in result
    assert "ВАЙБ" in result
    assert "Вайб бодрый" in result
    assert "Атмосфера: жиза и подколы" in result
    assert "Пересказ чата" in result
    assert "Тема дня" in result
    assert "Активные: @anya" in result


@pytest.mark.asyncio
async def test_build_manual_digest_ignores_threshold(session, monkeypatch):
    """D-02: /digest N не подавляется порогом digest_min_messages — в отличие
    от автодайджеста, явный запрос участника не спамит чат."""
    chat_id = -100920000005
    user_id = 700200005
    await _ensure_user(session, user_id, first_name="Боря")
    today = _today_msk()
    await _seed_daily_stat(session, chat_id, user_id, today, 1)  # ниже digest_min_messages
    await _seed_message(session, chat_id, user_id, datetime.now(timezone.utc), "одно сообщение")

    monkeypatch.setattr(digest_service.ai_client, "stream", _fake_stream)

    result = await digest_service.build_manual_digest(session, chat_id, days=1)

    assert result is not None
    assert "Дайджест чата" in result
