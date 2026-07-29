"""Дневной двойник (TWIN-03, запрошено 2026-07-27) — раз в MSK-день выбирает
случайного участника из уже согласившихся на `/twin` (`twin_opt_ins.status ==
'active'`, тот же consent-гейт, что и обычный `/twin` — намеренно НЕ отдельный
opt-in: команда не создавать второй список согласий под ту же по сути
мимикрию) и весь день:

- изредка постит от его имени случайные реплики в чат (проактивный тик,
  `register_daily_twin_tick`);
- отвечает на реплаи к своим же постам, на реплаи к РЕАЛЬНЫМ сообщениям
  сегодняшней персоны и на её @упоминания (`bot/handlers/daily_twin.py`,
  расширено 2026-07-28; контекстно через `twin_service.build_twin_reaction`).

Идемпотентный пик персоны дня — поверх общего `daily_pick_service`
(`kind="daily_twin"`, та же форма, что `victim_service`/`lottery_service`):
UNIQUE(chat_id, kind, day_msk) гарантирует одну и ту же персону весь день,
новый MSK-день — структурно свежая строка, явный сброс не нужен.

Критично (Pitfall 5/Critical Failure Mode #1 AI-SPEC): пик персоны дня НЕ
эквивалентен "согласие действует весь день" — согласие ПЕРЕПРОВЕРЯЕТСЯ на
КАЖДЫЙ пост/ответ, потому что и `build_twin_reply`, и `build_twin_reaction`
сами вызывают `_check_consent` первой строкой. Если человек поставит
`/twin_pause` в середине дня — дальнейшие посты от его имени в этот же день
просто прекращаются (`TwinConsentError` -> тихий скип ниже), а не продолжаются
до конца дня по уже отозванному согласию. Дневной пик в `daily_picks` при этом
никуда не девается (это просто "кто был выбран сегодня", не флаг согласия) —
если человек снова включит `/twin_resume` в тот же день, посты возобновятся.

Проактивный постинг — вероятностный ИНТЕРВАЛ-тик (не cron на фиксированное
время): на каждый тик в "рабочее" окно `_WINDOW_START_HOUR`-`_WINDOW_END_HOUR`
МСК бросается монета с шансом, подобранным так, чтобы в среднем за день
получалось `settings.daily_twin_posts_target` постов; `settings.
daily_twin_max_posts` — жёсткий потолок на статистический выброс (монета не
гарантирует ровно N, только матожидание N). Кандидаты/пик читаются заново на
каждом тике — не кешируются между тиками (день может смениться, согласие
может измениться).
"""

from __future__ import annotations

import logging
import secrets
from datetime import datetime

from aiogram import Bot
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import func
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import settings
from bot.services import daily_pick_service
from bot.services import settings_service
from bot.services import twin_service
from common.db.session import SessionLocal
from common.models.daily_twin_post import DailyTwinPost
from common.models.twin_opt_in import TwinOptIn
from common.models.user import User

logger = logging.getLogger(__name__)

# "Рабочее" окно проактивных постов — та же идея, что и у остальных
# ежедневных автопостов этого проекта (victim 10:00, lurker 12:00 — жёстко
# захардкожены, не settings), просто здесь не одна точка, а окно.
_WINDOW_START_HOUR = 9
_WINDOW_END_HOUR = 23
_TICK_MINUTES = 15
_TICK_JOB_ID = "daily_twin_tick"

_KIND = "daily_twin"

# Глобальный админ-рубильник фичи целиком (TWIN-03, запрошено 2026-07-29 —
# живой инцидент: персона дня оказалась активным участником треда,
# реактивный хендлер отвечал на каждый матчащий апдейт почти без остановки).
# Отдельно от /twin_pause/-resume (personal opt-out ОДНОГО участника из
# пика/чтения, twin_opt_ins) — это переключатель на ВЕСЬ чат разом,
# выключающий одновременно и проактивный тик (maybe_post_proactively), и
# реактивные ответы (bot/handlers/daily_twin.py), не трогая остальной
# функционал бота. Персистентно через bot_settings/settings_service (та же
# KV, что и /model_set) — переживает рестарт бота, не требует правки .env.
KEY_DAILY_TWIN_ENABLED = "daily_twin_enabled"

# server-authoritative RNG-seam (форма daily_pick_service._rng) —
# monkeypatchable в тестах.
_rng = secrets.SystemRandom()


async def is_enabled(session: AsyncSession, chat_id: int) -> bool:
    """Отсутствие строки в bot_settings = включено (текущее поведение по
    умолчанию для всех существующих чатов, обратная совместимость с тем,
    что было до этого рубильника)."""
    value = await settings_service.get_setting(session, chat_id, KEY_DAILY_TWIN_ENABLED, "1")
    return value != "0"


async def set_enabled(session: AsyncSession, chat_id: int, enabled: bool, updated_by_tg_id: int) -> None:
    """Не коммитит — та же дисциплина, что и у остального проекта
    (вызывающий, здесь — bot/handlers/daily_twin_admin.py, коммитит сам)."""
    await settings_service.set_setting(
        session, chat_id, KEY_DAILY_TWIN_ENABLED, "1" if enabled else "0", updated_by_tg_id
    )


def _tick_probability() -> float:
    """Шанс поста на ОДИН тик, подобранный так, чтобы за окно
    (_WINDOW_END_HOUR - _WINDOW_START_HOUR) часов матожидание числа постов
    равнялось settings.daily_twin_posts_target."""
    window_minutes = (_WINDOW_END_HOUR - _WINDOW_START_HOUR) * 60
    total_ticks = window_minutes / _TICK_MINUTES
    return settings.daily_twin_posts_target / total_ticks


async def _active_candidates(session: AsyncSession, chat_id: int) -> list[int]:
    result = await session.execute(
        select(TwinOptIn.user_id).where(
            TwinOptIn.chat_id == chat_id, TwinOptIn.status == "active"
        )
    )
    return [row[0] for row in result.all()]


async def get_todays_twin(session: AsyncSession, chat_id: int) -> tuple[int, str, str | None] | None:
    """(user_id, display_name, username) выбранной на сегодня персоны, либо
    None, если сегодня ЕЩЁ никого не выбирали И прямо сейчас нет ни одного
    активного согласия `/twin`, из которого выбрать. `username` нужен
    реактивному хендлеру (bot/handlers/daily_twin.py) для матчинга
    plain-@username-упоминаний (`text_mention`-упоминания Telegram уже
    резолвит в user-объект напрямую, username для них не нужен).

    Кандидаты резолвятся ДО вызова `daily_pick_service.get_or_set_pick`, но
    сам пик на сегодня — если он УЖЕ существует — возвращается независимо от
    того, пуст ли текущий список кандидатов: `get_or_set_pick` читает
    существующую строку ПЕРВЫМ делом и не трогает `candidates` вовсе, если
    строка найдена (см. её докстринг) — поэтому пустой список здесь безопасен
    для уже выбранного дня. `IndexError` из `_rng.choice([])` ловится ТОЛЬКО
    как сигнал "кандидатов нет И пика ещё нет" (единственный код-путь внутри
    `get_or_set_pick`, где пустой `candidates` реально используется) — если
    бы человек, выбранный СЕГОДНЯ, ПОЗЖЕ поставил `/twin_pause`, персона дня
    не должна "исчезать" из-за этого (см. модульный докстринг) — она
    и не исчезает, просто дальнейшая генерация упрётся в `TwinConsentError`
    внутри build_twin_reply/build_twin_reaction. Коммитит ТОЛЬКО при создании
    нового пика (та же дисциплина, что victim_service.run_victim —
    is_new-гейт), не при повторном чтении уже выбранной персоны."""
    candidates = await _active_candidates(session, chat_id)
    try:
        winner, is_new = await daily_pick_service.get_or_set_pick(
            session, chat_id, kind=_KIND, candidates=candidates
        )
    except IndexError:
        return None
    if is_new:
        await session.commit()

    row = (
        await session.execute(
            select(User.first_name, User.username).where(User.id == winner)
        )
    ).first()
    name = (row.first_name if row else None) or str(winner)
    username = row.username if row else None
    return winner, name, username


async def count_todays_posts(session: AsyncSession, chat_id: int) -> int:
    """Сколько постов дневного двойника уже ушло в этот чат сегодня — гейт
    `settings.daily_twin_max_posts` в проактивном тике ниже."""
    today = daily_pick_service._today_msk()
    result = await session.execute(
        select(func.count())
        .select_from(DailyTwinPost)
        .where(DailyTwinPost.chat_id == chat_id, DailyTwinPost.day_msk == today)
    )
    return result.scalar_one()


async def record_post(
    session: AsyncSession, chat_id: int, telegram_message_id: int, target_user_id: int
) -> None:
    """Журналирует отправленный пост (проактивный ИЛИ ответ на реплай) — не
    коммитит, транзакцию завершает вызывающий (та же дисциплина, что и у
    остального проекта)."""
    session.add(
        DailyTwinPost(
            chat_id=chat_id,
            telegram_message_id=telegram_message_id,
            target_user_id=target_user_id,
            day_msk=daily_pick_service._today_msk(),
        )
    )


async def find_target_by_post(session: AsyncSession, chat_id: int, telegram_message_id: int) -> int | None:
    """target_user_id поста двойника по его telegram_message_id, либо None,
    если это сообщение — не пост двойника (используется
    bot/handlers/daily_twin.py, чтобы отличить реплай НА ДВОЙНИКА от любого
    другого реплая в чате)."""
    return (
        await session.execute(
            select(DailyTwinPost.target_user_id).where(
                DailyTwinPost.chat_id == chat_id,
                DailyTwinPost.telegram_message_id == telegram_message_id,
            )
        )
    ).scalar_one_or_none()


def _in_posting_window(now_msk: datetime) -> bool:
    return _WINDOW_START_HOUR <= now_msk.hour < _WINDOW_END_HOUR


async def maybe_post_proactively(session: AsyncSession, chat_id: int, bot: Bot) -> bool:
    """Тело одного тика (вынесено из `register_daily_twin_tick`'s `_job()`
    отдельной именованной функцией — та же причина, что у
    `victim_service.run_victim`/`register_daily_autopost`: тестируемое ядро
    отдельно от тонкой scheduler-обвязки). Возвращает True, если реально
    запостил (для тестов/логирования), False на любой штатный скип
    (не окно, нет кандидатов, потолок постов, не выпала монета, AI не
    ответил). `TwinConsentError` НЕ ловится здесь — это осознанно (см.
    модульный докстринг): вызывающий (`_job`) отличает его от прочих ошибок."""
    if not await is_enabled(session, chat_id):
        return False

    now_msk = datetime.now(daily_pick_service.MSK)
    if not _in_posting_window(now_msk):
        return False

    twin = await get_todays_twin(session, chat_id)
    if twin is None:
        return False
    user_id, name, _username = twin

    posts_so_far = await count_todays_posts(session, chat_id)
    if posts_so_far >= settings.daily_twin_max_posts:
        return False

    if _rng.random() >= _tick_probability():
        return False

    text = await twin_service.build_twin_reply(session, chat_id, user_id, name)
    if text == twin_service.TWIN_FALLBACK_TEXT:
        # AI не смог ответить (reasoning-only модель/пустой ответ) — не
        # постим заглушку в чат ЯКОБЫ от лица человека, просто пробуем ещё
        # раз на следующем тике.
        return False

    # Голый текст, БЕЗ дисклеймер-префикса (запрошено 2026-07-28, явный отказ
    # от D-02/Pitfall 8 — владелец бота осознанно выбрал неразмеченный пост
    # вместо "🎭 Двойник дня — Имя:"). parse_mode не нужен — раньше HTML был
    # только ради разметки префикса, сырой AI-текст отправляется как есть.
    sent = await bot.send_message(chat_id, text)
    await record_post(session, chat_id, sent.message_id, user_id)
    await session.commit()
    return True


# --- register_daily_twin_tick (APScheduler interval-джоба) -------------------


def register_daily_twin_tick(scheduler: AsyncIOScheduler, bot: Bot) -> None:
    """Регистрирует вероятностный тик проактивных постов каждые
    `_TICK_MINUTES` минут (interval, НЕ cron — в отличие от
    victim_service.register_daily_autopost/lurker_service.register_daily_roast,
    здесь нужно несколько попыток за день, а не одна точка времени).
    coalesce+max_instances=1 — та же дисциплина, что у всех джоб проекта:
    пропущенные срабатывания не постят пачкой при догоняющем запуске."""

    async def _job() -> None:
        async with SessionLocal() as session:
            try:
                await maybe_post_proactively(session, settings.chat_id, bot)
            except twin_service.TwinConsentError:
                # Согласие отозвано/на паузе с момента выбора персоны дня —
                # штатный тихий скип (см. модульный докстринг), не ошибка.
                return
            except Exception:  # noqa: BLE001 - тик обязан пережить любую ошибку
                logger.exception("daily_twin_tick: тик упал")

    scheduler.add_job(
        _job,
        "interval",
        minutes=_TICK_MINUTES,
        coalesce=True,
        max_instances=1,
        misfire_grace_time=300,
        id=_TICK_JOB_ID,
        replace_existing=True,
    )
