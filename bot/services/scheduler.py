"""Singleton APScheduler для фоновых задач бота (единственный на весь репозиторий).

get_scheduler() лениво создаёт и возвращает один и тот же AsyncIOScheduler
(таймзона Europe/Moscow — все игровые/дайджест-сбросы идут по МСК).

setup_jobs(bot) — единая точка расширения: регистрирует фоновую NLP-
классификацию (nlp_classifier, interval 30с, NLP-02), эмбеддинг-воркер
(embed_worker, interval 45с) через их register(scheduler, bot),
автодайджест (digest_daily, cron hour=22 Europe/Moscow, D-01) через
digest_service.run_daily_digest, auto-close просроченных рынков ставок
(markets_auto_close, interval 5м) через markets_service.register_auto_close
(план 03-05), сверку/авторезолюцию внешних рынков Polymarket/Manifold
(external_markets_check, interval 30м) через
markets_service.register_external_check (план 03-06), авто-стенд
просроченных раздач блэкджека (blackjack_timeouts, interval 30с, D-07/D-08)
через casino_service.register_blackjack_timeouts (план 04.1-03),
mean-reversion тик AMM-пула фермы CP<->ювик (amm_mean_reversion, interval
10м, D-03) через clicker_service.register_amm_tick (план 04.1-05), демот
просроченных Telegram custom_title + восстановление подвешенной аренды
(active_titles_expire, interval 5м, D-07/D-10) через
tag_service.register_title_expiry (план 05-03), автопост /awards
(awards_daily_autopost, cron ~23:55 МСК, AWARDS-01/02) через
awards_service.register_daily_autopost (план 05-06), и проактивный
сброс/анонс лотереи «Yuvi_Yuvi дня» (lottery_daily_reset, cron 00:00 МСК,
LOTTERY-01, UX-safety-net поверх day_msk из Pitfall 4) через
lottery_service.register_daily_reset (план 05-05). Импорты
ленивые (внутри функции), чтобы модули, ещё не существующие на момент
плана 01 (пустой setup_jobs), не ломали import bot.main до их появления.
"""

from __future__ import annotations

from zoneinfo import ZoneInfo

from aiogram import Bot
from apscheduler.schedulers.asyncio import AsyncIOScheduler

MSK = ZoneInfo("Europe/Moscow")

_scheduler: AsyncIOScheduler | None = None


def get_scheduler() -> AsyncIOScheduler:
    """Возвращает singleton AsyncIOScheduler (создаёт при первом вызове)."""
    global _scheduler
    if _scheduler is None:
        _scheduler = AsyncIOScheduler(timezone=MSK)
    return _scheduler


_DIGEST_JOB_ID = "digest_daily"


def setup_jobs(bot: Bot) -> None:
    """Точка расширения для фоновых job'ов.

    Регистрирует NLP-классификацию (NLP-02), эмбеддинг-воркер (план 05),
    автодайджест (план 09, D-01: раз в день, 22:00 МСК), auto-close
    просроченных рынков ставок (план 03-05, markets_auto_close, interval
    5м), сверку/авторезолюцию внешних рынков (план 03-06,
    external_markets_check, interval 30м), авто-стенд просроченных раздач
    блэкджека (план 04.1-03, blackjack_timeouts, interval 30с, D-07/D-08),
    mean-reversion тик AMM-пула фермы (план 04.1-05, amm_mean_reversion,
    interval 10м, D-03), демот просроченных Telegram custom_title +
    восстановление подвешенной аренды (план 05-03, active_titles_expire,
    interval 5м, D-07/D-10), автопост /awards (план 05-06,
    awards_daily_autopost, cron ~23:55 МСК, AWARDS-01/02) и сброс/анонс
    ежедневной лотереи (план 05-05, lottery_daily_reset, cron 00:00 МСК,
    LOTTERY-01). Ленивые импорты —
    эти модули появились в более поздних планах, чем изначальный (пустой)
    setup_jobs плана 01.
    """
    from bot.services import awards_service
    from bot.services import casino_service
    from bot.services import clicker_service
    from bot.services import digest_service
    from bot.services import embed_worker
    from bot.services import lottery_service
    from bot.services import markets_service
    from bot.services import nlp_classifier
    from bot.services import tag_service

    scheduler = get_scheduler()
    nlp_classifier.register(scheduler, bot)
    embed_worker.register(scheduler, bot)
    markets_service.register_auto_close(scheduler)
    markets_service.register_external_check(scheduler)
    casino_service.register_blackjack_timeouts(scheduler)
    clicker_service.register_amm_tick(scheduler)
    tag_service.register_title_expiry(scheduler, bot)
    awards_service.register_daily_autopost(scheduler, bot)
    lottery_service.register_daily_reset(scheduler, bot)

    async def _digest_job() -> None:
        await digest_service.run_daily_digest(bot)

    # D-01/D-02: раз в день в 22:00 МСК. coalesce+max_instances=1 — если бот
    # был офлайн и APScheduler видит несколько пропущенных срабатываний,
    # выполняется только одно, не шлём дайджест N раз (T-02-22). misfire_grace_time
    # даёт запуск, даже если бот поднялся с опозданием в пределах часа.
    scheduler.add_job(
        _digest_job,
        "cron",
        hour=22,
        minute=0,
        timezone=MSK,
        coalesce=True,
        max_instances=1,
        misfire_grace_time=3600,
        id=_DIGEST_JOB_ID,
        replace_existing=True,
    )
