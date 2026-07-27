"""Дайджест чата (AI-02, D-01/D-02/D-03/D-12, формат "Дайджест чата" 2026-07-27).

build_digest(session, chat_id) — за СЕГОДНЯШНИЙ день (Europe/Moscow): если
активность чата ниже settings.digest_min_messages — возвращает None БЕЗ
единого обращения к LLM (D-03/D-12, T-02-21 — чат не спамится пустым/скудным
автопостом, экономия платных вызовов).

build_manual_digest(session, chat_id, days) — та же сборка для ручного
/digest N, но БЕЗ порога D-03: явный запрос участника не спамит чат.

Обе функции строят единый расширенный формат: числовая шапка (период/всего
сообщений/активные участники/топ авторов/горячие окна), ВСПЛЕСКИ (LLM-блёрб
на каждое "горячее окно" — скользящее окно по объёму сообщений,
detect_bursts), ЦИТАТЫ (по одному самому длинному сообщению из каждого
всплеска — алгоритмически, без LLM), ВАЙБ (короткий LLM-нарратив в стиле
"+100500", мат уместен — участники 18+) + Атмосфера (короткий тег той же
LLM-генерации), Пересказ (структурированный LLM-пересказ последних
digest_recap_message_limit сообщений периода — нумерованные темы с цитатами,
не единый абзац) и список активных участников.

Числовая шапка и обе "тяжёлые" метрики (всего сообщений, топ авторов) намеренно
читают РАЗНЫЕ источники: "Всего сообщений" — daily_stats (D-06 конвенция,
включает ЛЮБые типы сообщений), а bursts/quotes/recap/топ-авторов — один
общий SELECT по messages с text IS NOT NULL (нужен реальный текст и время
каждого сообщения периода, чего daily_stats не несёт). Небольшое расхождение
между "официальным" total_messages и числом текстовых сообщений, видных
детектору всплесков, ожидаемо (медиа без подписи считаются в первом, но не
во втором) и не является багом.

run_daily_digest(bot) — обёртка для cron-job'а (регистрируется scheduler.py):
сама открывает AsyncSession через SessionLocal (job не имеет доступа к
middleware-сессии запроса), как nlp_classifier/embed_worker; broad-except +
logger.exception — падение дайджеста не должно ронять планировщик.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from dataclasses import field
from datetime import date
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from zoneinfo import ZoneInfo

from aiogram import Bot
from sqlalchemy import func
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import settings
from bot.services import ai_client
from bot.services import settings_service
from bot.services.summary_service import CHARS_PER_TOKEN
from bot.services.summary_service import build_context
from common.db.session import SessionLocal
from common.models.daily_stat import DailyStat
from common.models.message import Message
from common.models.user import User

logger = logging.getLogger(__name__)

MSK = ZoneInfo("Europe/Moscow")


def _today_msk() -> date:
    return datetime.now(MSK).date()


async def count_day_messages(session: AsyncSession, chat_id: int, day: date) -> int:
    """Число сообщений чата за КОНКРЕТНЫЙ день (Europe/Moscow) — прямой
    запрос к daily_stats по точной дате (не диапазон), не COUNT(*) по
    messages (RESEARCH.md Anti-Patterns)."""
    stmt = select(func.coalesce(func.sum(DailyStat.message_count), 0)).where(
        DailyStat.chat_id == chat_id,
        DailyStat.stat_date == day,
    )
    result = await session.execute(stmt)
    return int(result.scalar_one())


async def count_period_messages(session: AsyncSession, chat_id: int, since_day: date, until_day: date) -> int:
    """Число сообщений чата за [since_day, until_day] включительно (Europe/Moscow),
    из daily_stats — та же дисциплина, что count_day_messages."""
    stmt = select(func.coalesce(func.sum(DailyStat.message_count), 0)).where(
        DailyStat.chat_id == chat_id,
        DailyStat.stat_date >= since_day,
        DailyStat.stat_date <= until_day,
    )
    result = await session.execute(stmt)
    return int(result.scalar_one())


def _msk_day_bounds_utc(since_day: date, until_day: date) -> tuple[datetime, datetime]:
    """[since_day 00:00 МСК, until_day+1 00:00 МСК) в наивном UTC — messages.created_at
    хранится наивным UTC (см. common/models/message.py)."""
    start_msk = datetime.combine(since_day, datetime.min.time(), tzinfo=MSK)
    end_msk = datetime.combine(until_day + timedelta(days=1), datetime.min.time(), tzinfo=MSK)
    start_utc = start_msk.astimezone(timezone.utc).replace(tzinfo=None)
    end_utc = end_msk.astimezone(timezone.utc).replace(tzinfo=None)
    return start_utc, end_utc


async def fetch_period_messages(
    session: AsyncSession, chat_id: int, since_day: date, until_day: date
) -> list[dict]:
    """Текстовые сообщения чата за период [since_day, until_day] включительно
    (Europe/Moscow), в хронологическом порядке, с именем/username автора —
    единый источник для detect_bursts/цитат/топ-авторов/пересказа (см.
    модульный докстринг про расхождение с daily_stats.message_count)."""
    start_utc, end_utc = _msk_day_bounds_utc(since_day, until_day)
    stmt = (
        select(
            Message.user_id,
            User.first_name,
            User.username,
            Message.text,
            Message.created_at,
        )
        .outerjoin(User, User.id == Message.user_id)
        .where(
            Message.chat_id == chat_id,
            Message.text.is_not(None),
            Message.created_at >= start_utc,
            Message.created_at < end_utc,
        )
        .order_by(Message.created_at.asc(), Message.id.asc())
    )
    result = await session.execute(stmt)
    return [
        {
            "user_id": row.user_id,
            "first_name": row.first_name,
            "username": row.username,
            "text": row.text,
            "created_at": row.created_at,
        }
        for row in result.all()
    ]


def _display_name(msg: dict) -> str:
    if msg["username"]:
        return f"@{msg['username']}"
    return msg["first_name"] or "Аноним"


# --- Горячие окна (ВСПЛЕСКИ) --------------------------------------------------


@dataclass
class Burst:
    """Одно "горячее окно" — непрерывный отрезок времени с аномально высоким
    объёмом сообщений."""

    start: datetime  # наивный UTC
    end: datetime  # наивный UTC
    count: int
    messages: list[dict] = field(default_factory=list)


def detect_bursts(
    messages: list[dict],
    window_minutes: int | None = None,
    bucket_minutes: int | None = None,
    top_k: int | None = None,
    min_count: int | None = None,
) -> list[Burst]:
    """Топ до top_k НЕПЕРЕСЕКАЮЩИХСЯ скользящих окон длиной window_minutes с
    наибольшим объёмом сообщений, отбор с шагом bucket_minutes (для
    производительности — не проверяем окно на каждую миллисекунду, а бакетируем
    сообщения и суммируем по бакетам). Окна короче min_count сообщений не
    считаются всплеском (тихий день не должен показывать "всплески" из 2
    сообщений). Возвращает окна в ХРОНОЛОГИЧЕСКОМ порядке (не по убыванию
    объёма) — так их удобно нумеровать B1..BN по ходу дня.

    Жадный отбор: сортируем окна-кандидаты по объёму убыв., берём самое
    "горячее", исключаем все окна, пересекающиеся с уже выбранными, повторяем.
    Не оптимальный (не максимизирует суммарный охват), но детерминированный и
    простой — для дайджеста важна наглядность топ-N моментов, не идеальное
    покрытие."""
    window_minutes = window_minutes or settings.digest_burst_window_minutes
    bucket_minutes = bucket_minutes or settings.digest_burst_bucket_minutes
    top_k = top_k or settings.digest_burst_top_k
    min_count = settings.digest_burst_min_messages if min_count is None else min_count

    if not messages:
        return []

    sorted_msgs = sorted(messages, key=lambda m: m["created_at"])
    origin = sorted_msgs[0]["created_at"]
    bucket = timedelta(minutes=bucket_minutes)
    buckets_per_window = max(1, window_minutes // bucket_minutes)

    bucket_msgs: dict[int, list[dict]] = {}
    for msg in sorted_msgs:
        idx = int((msg["created_at"] - origin) / bucket)
        bucket_msgs.setdefault(idx, []).append(msg)

    max_idx = max(bucket_msgs)
    window_counts: list[tuple[int, int]] = []  # (start_bucket_idx, count)
    for start_idx in range(max_idx + 1):
        count = sum(len(bucket_msgs.get(j, [])) for j in range(start_idx, start_idx + buckets_per_window))
        if count > 0:
            window_counts.append((start_idx, count))

    candidates = sorted(window_counts, key=lambda x: -x[1])

    selected: list[tuple[int, int, int]] = []  # (start_idx, end_idx, count)
    used_ranges: list[tuple[int, int]] = []
    for start_idx, count in candidates:
        if count < min_count:
            break
        end_idx = start_idx + buckets_per_window - 1
        if any(not (end_idx < u_start or start_idx > u_end) for u_start, u_end in used_ranges):
            continue
        selected.append((start_idx, end_idx, count))
        used_ranges.append((start_idx, end_idx))
        if len(selected) >= top_k:
            break

    bursts = []
    for start_idx, end_idx, count in selected:
        window_start = origin + start_idx * bucket
        window_end = origin + (end_idx + 1) * bucket
        window_msgs = [m for j in range(start_idx, end_idx + 1) for m in bucket_msgs.get(j, [])]
        bursts.append(Burst(start=window_start, end=window_end, count=count, messages=window_msgs))

    bursts.sort(key=lambda b: b.start)
    return bursts


def _msk(dt: datetime) -> datetime:
    return dt.replace(tzinfo=timezone.utc).astimezone(MSK)


def _format_bursts_line(bursts: list[Burst]) -> str:
    if not bursts:
        return "Горячие окна: не найдено."
    parts = []
    for i, burst in enumerate(bursts, start=1):
        start_msk, end_msk = _msk(burst.start), _msk(burst.end)
        parts.append(
            f"B{i} {start_msk:%m-%d} {start_msk:%H:%M}–{end_msk:%H:%M} ({burst.count})"
        )
    return "Горячие окна: " + " · ".join(parts)


# --- ВСПЛЕСКИ: LLM-блёрб на каждое горячее окно --------------------------------

_BURST_BLURB_SYSTEM_SUFFIX = (
    "\n\nНиже — несколько «горячих окон» переписки чата (периоды аномально "
    "высокой активности), пронумерованных. Для КАЖДОГО окна одной строкой "
    "опиши, что там обсуждали и кто доминировал в разговоре — коротко (1-2 "
    "предложения), энергично, иронично, в стиле «+100500» (мат уместен, "
    "участники чата 18+). Ответь СТРОГО построчно, каждая строка в формате "
    "\"N: текст\", где N — номер окна, БЕЗ дополнительных пояснений до/после."
)

_BURST_BLURB_LINE_RE = re.compile(r"^\s*(\d+)\s*[:.\-]\s*(.+)$")


def _build_burst_prompt_context(bursts: list[Burst], char_budget: int) -> str:
    blocks = []
    for i, burst in enumerate(bursts, start=1):
        lines = [f"Окно {i}:"]
        lines.extend(f"{_display_name(m)}: {m['text']}" for m in burst.messages)
        blocks.append("\n".join(lines))
    context = "\n\n".join(blocks)
    return context[-char_budget:] if len(context) > char_budget else context


async def build_burst_blurbs(session: AsyncSession, chat_id: int, bursts: list[Burst]) -> list[str]:
    """Один LLM-вызов на ВСЕ окна сразу (не по одному на окно — иначе N
    отдельных платных вызовов на дайджест). При сбое парсинга (модель не
    вернула ожидаемое число строк) — недостающие окна получают заглушку, не
    падаем и не роняем весь дайджест из-за одного окна."""
    if not bursts:
        return []

    char_budget = settings.ai_max_input_tokens * CHARS_PER_TOKEN
    context = _build_burst_prompt_context(bursts, char_budget)

    system_prompt = await settings_service.get_active_prompt(session, chat_id)
    system_prompt += _BURST_BLURB_SYSTEM_SUFFIX
    model = await settings_service.get_active_model(session, chat_id)
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": context},
    ]

    parts = [
        delta
        async for delta in ai_client.stream(messages, model=model, max_tokens=settings.ai_max_output_tokens)
    ]
    raw = "".join(parts).strip()

    blurbs_by_index: dict[int, str] = {}
    for line in raw.splitlines():
        match = _BURST_BLURB_LINE_RE.match(line)
        if match:
            blurbs_by_index[int(match.group(1))] = match.group(2).strip()

    return [blurbs_by_index.get(i, "не удалось получить описание.") for i in range(1, len(bursts) + 1)]


def _format_burst_section(bursts: list[Burst], blurbs: list[str]) -> str:
    if not bursts:
        return "ВСПЛЕСКИ\nНе найдено — период слишком тихий для горячих окон."
    lines = ["ВСПЛЕСКИ"]
    for i, (burst, blurb) in enumerate(zip(bursts, blurbs), start=1):
        start_msk, end_msk = _msk(burst.start), _msk(burst.end)
        lines.append(
            f"[BURST {i}: {start_msk:%Y-%m-%d}, {start_msk:%H:%M}–{end_msk:%H:%M} МСК] — {blurb}"
        )
    return "\n".join(lines)


# --- ЦИТАТЫ: самое длинное сообщение каждого всплеска (без LLM) --------------


def pick_burst_quotes(bursts: list[Burst], min_length: int = 40) -> list[dict]:
    """Одна цитата на всплеск — самое длинное сообщение окна (простая, но
    надёжная эвристика "заметности": не зовём LLM второй раз только чтобы
    выбрать цитату, и не рискуем, что модель придумает цитату, которой не
    было). Окна без сообщений длиннее min_length пропускаются (короткие
    реплики плохо смотрятся как цитата)."""
    quotes = []
    for i, burst in enumerate(bursts, start=1):
        candidates = [m for m in burst.messages if m["text"] and len(m["text"]) >= min_length]
        if not candidates:
            continue
        longest = max(candidates, key=lambda m: len(m["text"]))
        quotes.append({"burst_index": i, **longest})
    return quotes


def _format_quotes_section(quotes: list[dict]) -> str:
    if not quotes:
        return "ЦИТАТЫ\nНечего процитировать — сообщения периода слишком короткие."
    lines = ["ЦИТАТЫ"]
    for quote in quotes:
        time_msk = _msk(quote["created_at"])
        lines.append(f"[Q{quote['burst_index']}] {_display_name(quote)} ({time_msk:%H:%M}): {quote['text']}")
    return "\n".join(lines)


# --- ВАЙБ + Атмосфера: один короткий LLM-нарратив -----------------------------

_VIBE_SYSTEM_SUFFIX = (
    "\n\nНа основе переписки чата опиши ОБЩЕЕ настроение и атмосферу периода "
    "— живо, с придурью и мемами, как оно реально ощущается в чате, а не "
    "сухим канцеляритом; энергично и иронично, в стиле «+100500» (мат "
    "уместен, участники чата 18+). Ответь СТРОГО в этом формате:\n"
    "ВАЙБ\n(2-4 предложения — общее настроение/вайб периода)\n"
    "АТМОСФЕРА: (одна короткая фраза-тег, 3-8 слов)"
)

_ATMOSPHERE_LINE_RE = re.compile(r"^АТМОСФЕРА:\s*(.+)$", re.MULTILINE)


async def build_vibe(session: AsyncSession, chat_id: int, sample_context: str) -> dict:
    """ВАЙБ (нарратив) + Атмосфера (короткий тег) одним LLM-вызовом. При
    сбое парсинга атмосферы — тег остаётся пустым (не падаем, просто не
    показываем строку "Атмосфера" в композиции)."""
    system_prompt = await settings_service.get_active_prompt(session, chat_id)
    system_prompt += _VIBE_SYSTEM_SUFFIX
    model = await settings_service.get_active_model(session, chat_id)
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": sample_context or "Сообщений нет."},
    ]

    parts = [
        delta
        async for delta in ai_client.stream(messages, model=model, max_tokens=settings.ai_max_output_tokens)
    ]
    raw = "".join(parts).strip()

    atmosphere_match = _ATMOSPHERE_LINE_RE.search(raw)
    atmosphere = atmosphere_match.group(1).strip() if atmosphere_match else ""
    vibe_text = raw[: atmosphere_match.start()].strip() if atmosphere_match else raw
    vibe_text = re.sub(r"^ВАЙБ\s*\n?", "", vibe_text).strip()

    return {"vibe": vibe_text or "Вайб не удалось поймать — попробуйте позже.", "atmosphere": atmosphere}


def _format_vibe_section(vibe: dict) -> str:
    lines = ["ВАЙБ", vibe["vibe"]]
    return "\n".join(lines)


# --- Пересказ: структурированный LLM-пересказ последних N сообщений ----------

_RECAP_SYSTEM_SUFFIX = (
    "\n\nСделай структурированный пересказ этой переписки на русском языке — "
    "энергично, иронично, в стиле «+100500» (мат уместен, участники чата "
    "18+). Раздели на 3-6 нумерованных тем/эпизодов, для каждой темы одна "
    "строка вида \"N. **Короткое название темы** — описание с 1-2 цитатами "
    "участников в кавычках, указывая @username или имя перед цитатой\". Не "
    "пиши общих фраз без цитат — опирайся на реальные реплики из переписки."
)


async def build_structured_recap(session: AsyncSession, chat_id: int, rows: list[dict]) -> str:
    """Нумерованные темы + цитаты по последним rows сообщениям (уже
    отфильтрованным/обрезанным вызывающим до digest_recap_message_limit)."""
    if not rows:
        return "Пересказ чата\nСообщений нет."

    char_budget = settings.ai_max_input_tokens * CHARS_PER_TOKEN
    context_rows = [{"author": _display_name(m), "text": m["text"]} for m in rows]
    context = build_context(context_rows, char_budget)

    system_prompt = await settings_service.get_active_prompt(session, chat_id)
    system_prompt += _RECAP_SYSTEM_SUFFIX
    model = await settings_service.get_active_model(session, chat_id)
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": context or "Сообщений нет."},
    ]

    parts = [
        delta
        async for delta in ai_client.stream(messages, model=model, max_tokens=settings.ai_max_output_tokens)
    ]
    text = "".join(parts).strip()
    return f"Пересказ чата\n{text}" if text else "Пересказ чата\nПересказ недоступен — недостаточно сообщений."


# --- Числовая шапка ------------------------------------------------------------


def _format_header(
    since_day: date,
    until_day: date,
    total_messages: int,
    top_authors: list[tuple[str, int]],
) -> str:
    days = (until_day - since_day).days + 1
    active_count = len(top_authors)
    period_line = (
        f"Период: {since_day} — {until_day} ({days} дн.)"
        if since_day != until_day
        else f"Период: {since_day} (1 дн.)"
    )
    authors_line = "Топ авторов: " + ", ".join(f"{name} ({count})" for name, count in top_authors[:5])
    return "\n".join(
        [
            "Дайджест чата",
            "",
            period_line,
            f"Всего сообщений: {total_messages} | Активных участников: {active_count}",
            authors_line,
        ]
    )


def _top_authors(messages: list[dict], limit: int = 5) -> list[tuple[str, int]]:
    counts: dict[tuple, int] = {}
    for m in messages:
        key = (m["user_id"], _display_name(m))
        counts[key] = counts.get(key, 0) + 1
    ranked = sorted(counts.items(), key=lambda kv: -kv[1])
    return [(name, count) for (_, name), count in ranked[:limit]]


def _active_participants_line(messages: list[dict], limit: int = 15) -> str:
    seen: dict = {}
    for m in messages:
        seen.setdefault(m["user_id"], _display_name(m))
    names = list(seen.values())[:limit]
    return "Активные: " + ", ".join(names) if names else "Активные: нет данных."


# --- Сборка ---------------------------------------------------------------------


async def _build_digest_body(session: AsyncSession, chat_id: int, since_day: date, until_day: date) -> str:
    """Общее ядро сборки дайджеста за произвольный период — используется и
    автодайджестом (since_day == until_day == сегодня), и ручным /digest N."""
    total_messages = await count_period_messages(session, chat_id, since_day, until_day)
    period_messages = await fetch_period_messages(session, chat_id, since_day, until_day)

    top_authors = _top_authors(period_messages, limit=5)
    header = _format_header(since_day, until_day, total_messages, top_authors)

    bursts = detect_bursts(period_messages)
    bursts_line = _format_bursts_line(bursts)
    blurbs = await build_burst_blurbs(session, chat_id, bursts)
    burst_section = _format_burst_section(bursts, blurbs)

    quotes = pick_burst_quotes(bursts)
    quotes_section = _format_quotes_section(quotes)

    vibe_sample_rows = [{"author": _display_name(m), "text": m["text"]} for m in period_messages]
    vibe_char_budget = settings.ai_max_input_tokens * CHARS_PER_TOKEN
    vibe_sample = build_context(vibe_sample_rows, vibe_char_budget)
    vibe = await build_vibe(session, chat_id, vibe_sample)
    vibe_section = _format_vibe_section(vibe)

    recap_rows = period_messages[-settings.digest_recap_message_limit :]
    recap_section = await build_structured_recap(session, chat_id, recap_rows)

    active_line = _active_participants_line(period_messages)

    sections = [
        header,
        bursts_line,
        "",
        burst_section,
        "",
        quotes_section,
        "",
        vibe_section,
        "",
        recap_section,
        "",
        active_line,
    ]
    if vibe["atmosphere"]:
        sections.append(f"Атмосфера: {vibe['atmosphere']}")

    return "\n".join(sections)


async def build_digest(session: AsyncSession, chat_id: int) -> str | None:
    """Автодайджест за СЕГОДНЯШНИЙ день (Europe/Moscow).

    D-03/D-12: если активность за день < settings.digest_min_messages —
    возвращает None БЕЗ единого обращения к LLM (T-02-21) — вызывающий код
    (run_daily_digest) не должен слать сообщение в чат.
    """
    today = _today_msk()
    count = await count_day_messages(session, chat_id, today)
    if count < settings.digest_min_messages:
        return None

    return await _build_digest_body(session, chat_id, today, today)


async def build_manual_digest(session: AsyncSession, chat_id: int, days: int = 1) -> str:
    """Ручной /digest N (D-02) — БЕЗ порога D-03/D-12: явный вызов участника,
    чат не может "заспамить сам себя" автопостом, поэтому digest_min_messages
    здесь намеренно не проверяется (порог применяется только к
    автодайджесту, build_digest)."""
    until_day = _today_msk()
    since_day = until_day - timedelta(days=max(days, 1) - 1)
    return await _build_digest_body(session, chat_id, since_day, until_day)


async def run_daily_digest(bot: Bot) -> None:
    """Обёртка для cron-job'а (D-01, 22:00 МСК): сама открывает сессию через
    SessionLocal, шлёт результат build_digest в settings.chat_id, если он не
    None (D-03). broad-except + logger — падение дайджеста не должно ронять
    планировщик."""
    try:
        async with SessionLocal() as session:
            text = await build_digest(session, settings.chat_id)
        if text is not None:
            await bot.send_message(settings.chat_id, text)
    except Exception:  # noqa: BLE001 - job обязан пережить любую ошибку и не уронить планировщик
        logger.exception("run_daily_digest: не удалось собрать/отправить дайджест")
