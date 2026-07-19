"""Ежедневный `/awards` (AWARDS-01/AWARDS-02) — 7 номинаций: 6 детерминированных
user-номинаций из `daily_stats` (главный по сообщениям, матершинник, фото,
форварды, самое длинное, наименее активный) + 1 инфо-номинация «случайная
игра из Steam Wishlist».

Скоринг читает ГОТОВЫЙ агрегат `daily_stats` (`ORDER BY метрика ... LIMIT 1`)
— никакого `COUNT(*)` по `messages` на query-time (05-RESEARCH.md
Anti-Pattern, тот же принцип, что `stats_service.py`/`victim_service.py`).

Победители получают мемные суммы (322 главный / 228 остальные) ИЗ БАНКА
чата через `economy_service.pay_from_bank` — идемпотентно по
`ref_id=f"award:{chat_id}:{day_msk}:{key}"` (AWARDS-02/D-09): повторный
`/awards` (ручной или автопост) в тот же MSK-день не платит повторно,
победители и суммы остаются теми же.

Этот модуль самодостаточен: собственный `_today_msk()` (monkeypatchable
seam, форма `daily_pick_service._today_msk`), НЕ импортирует
`daily_pick_service` (05-04) — номинации детерминированы (никакого рандома,
кроме Steam-игры, которая живёт в `steam_service` со своим собственным
day-seeded детерминизмом), поэтому идемпотентность выплат целиком
обеспечивает `ref_id`, отдельная `daily_picks`-строка не нужна.
"""

from __future__ import annotations

import html
import logging
from datetime import date
from datetime import datetime
from zoneinfo import ZoneInfo

from aiogram import Bot
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import settings
from bot.services import economy_service
from bot.services import steam_service
from common.db.session import SessionLocal
from common.models.daily_stat import DailyStat
from common.models.user import User

logger = logging.getLogger(__name__)

MSK = ZoneInfo("Europe/Moscow")

AWARD_PRIZE_MAIN = 322
AWARD_PRIZE_MEME = 228

# (key, daily_stats-колонка, направление, приз, русский label).
NOMINATIONS: tuple[dict, ...] = (
    {
        "key": "most_messages",
        "column": DailyStat.message_count,
        "direction": "desc",
        "prize": AWARD_PRIZE_MAIN,
        "label": "Главный по сообщениям",
    },
    {
        "key": "profanity",
        "column": DailyStat.profanity_count,
        "direction": "desc",
        "prize": AWARD_PRIZE_MEME,
        "label": "Матершинник дня",
    },
    {
        "key": "photos",
        "column": DailyStat.photo_count,
        "direction": "desc",
        "prize": AWARD_PRIZE_MEME,
        "label": "Фотограф дня",
    },
    {
        "key": "forwards",
        "column": DailyStat.forward_count,
        "direction": "desc",
        "prize": AWARD_PRIZE_MEME,
        "label": "Репостер дня",
    },
    {
        "key": "longest",
        "column": DailyStat.longest_msg_len,
        "direction": "desc",
        "prize": AWARD_PRIZE_MEME,
        "label": "Самое длинное сообщение",
    },
    {
        "key": "least_active",
        "column": DailyStat.message_count,
        "direction": "asc",
        "prize": AWARD_PRIZE_MEME,
        "label": "Наименее активный",
    },
)


def _today_msk() -> date:
    """MSK-day seam — собственный, не переиспользует daily_pick_service
    (awards_service самодостаточен, см. модульный docstring)."""
    return datetime.now(MSK).date()


async def compute_nominations(session: AsyncSession, chat_id: int, day_msk: date) -> list[dict]:
    """Считает победителя каждой из 6 номинаций по `daily_stats` за
    `day_msk`. Тай-брейк — `user_id ASC` (детерминированный, не рандом).
    DESC-номинации с победной метрикой `0` (или отсутствием строк за день)
    возвращают `winner_user_id=None` — "никто", выплата не производится
    (пустая номинация не должна награждать случайного участника с нулём).
    """
    results: list[dict] = []
    for nom in NOMINATIONS:
        order = nom["column"].desc() if nom["direction"] == "desc" else nom["column"].asc()
        stmt = (
            select(DailyStat.user_id, nom["column"])
            .where(DailyStat.chat_id == chat_id, DailyStat.stat_date == day_msk)
            .order_by(order, DailyStat.user_id.asc())
            .limit(1)
        )
        row = (await session.execute(stmt)).first()

        winner_user_id: int | None = None
        metric_value = 0
        if row is not None:
            metric_value = row[1]
            # Пустая DESC-номинация (метрика 0 у всех/нет активности) — не
            # награждаем никого; least_active (ASC) всегда имеет победителя,
            # если за день была хоть одна активная строка.
            if nom["direction"] == "desc" and metric_value <= 0:
                winner_user_id = None
            else:
                winner_user_id = row[0]

        results.append(
            {
                "key": nom["key"],
                "label": nom["label"],
                "prize": nom["prize"],
                "winner_user_id": winner_user_id,
                "metric_value": metric_value,
            }
        )
    return results


async def run_awards(session: AsyncSession, chat_id: int) -> dict:
    """Считает номинации дня + выплачивает победителям из банка (идемпотентно
    по ref_id, AWARDS-02) + резолвит Steam-игру дня. Коммитит.

    Возвращает {day_msk, nominations: [...], steam_game}. Каждый элемент
    `nominations` дополняется `paid` (фактически выплаченная сумма — 0, если
    победителя нет ИЛИ ref_id уже применялся ранее в этот день, AWARDS-02).
    """
    day_msk = _today_msk()
    nominations = await compute_nominations(session, chat_id, day_msk)

    for nom in nominations:
        paid = 0
        if nom["winner_user_id"] is not None:
            paid = await economy_service.pay_from_bank(
                session,
                chat_id,
                nom["winner_user_id"],
                nom["prize"],
                kind=f"award_{nom['key']}",
                ref_id=f"award:{chat_id}:{day_msk}:{nom['key']}",
            )
        nom["paid"] = paid

    steam_game = await steam_service.get_random_wishlist_game(day_msk)

    await session.commit()

    return {"day_msk": day_msk, "nominations": nominations, "steam_game": steam_game}


async def format_awards_post(session: AsyncSession, result: dict) -> str:
    """Рендерит единый пост `/awards`: заголовок + строка на номинацию
    (label + html.escape(имя победителя) + сумма, либо «никто») + строка
    Steam-игры дня (или fallback «Steam недоступен 😢»). Общая функция для
    ручного `/awards` (bot/handlers/awards.py) и автопоста (`_job` ниже) —
    единственное место форматирования, без дублирования (D-04 приоритет
    reuse over duplicate)."""
    lines = ["🏆 <b>Итоги дня</b>"]
    for nom in result["nominations"]:
        if nom["winner_user_id"] is None:
            lines.append(f"{nom['label']}: никто")
            continue
        name = (
            await session.execute(
                select(User.first_name).where(User.id == nom["winner_user_id"])
            )
        ).scalar_one_or_none() or str(nom["winner_user_id"])
        lines.append(f"{nom['label']}: {html.escape(name)} — {nom['prize']} ювиков")

    if result["steam_game"] is not None:
        lines.append(f"🎮 Игра дня из Steam Wishlist: {html.escape(result['steam_game'])}")
    else:
        lines.append("🎮 Steam недоступен 😢")

    return "\n".join(lines)


# --- register_daily_autopost (APScheduler, форма scheduler.py::_digest_job) --

_AUTOPOST_JOB_ID = "awards_daily_autopost"


def register_daily_autopost(scheduler: AsyncIOScheduler, bot: Bot) -> None:
    """Регистрирует автопост `/awards` ~23:55 МСК (cron), по образцу
    `_digest_job`/`tag_service.register_title_expiry`: своя `SessionLocal`,
    broad-except — тик обязан пережить любую ошибку и не уронить
    планировщик. `coalesce+max_instances=1` — пропущенные срабатывания (бот
    был офлайн) не постят несколько раз (T-02-22 прецедент)."""

    async def _job() -> None:
        async with SessionLocal() as session:
            try:
                result = await run_awards(session, settings.chat_id)
                text = await format_awards_post(session, result)
                await bot.send_message(settings.chat_id, text, parse_mode="HTML")
            except Exception:  # noqa: BLE001 - тик обязан пережить любую ошибку
                logger.exception("awards_daily_autopost: тик упал")

    scheduler.add_job(
        _job,
        "cron",
        hour=23,
        minute=55,
        timezone=MSK,
        coalesce=True,
        max_instances=1,
        misfire_grace_time=3600,
        id=_AUTOPOST_JOB_ID,
        replace_existing=True,
    )
