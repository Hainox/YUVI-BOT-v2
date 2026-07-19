"""Единственный владелец Telegram custom_title и таблицы `active_titles`
(TAG-01/02, D-07/D-10) — жертва дня (source='victim', план 05-04) и рынок
аренды тегов (source='rental', план 05-07) строятся поверх этого модуля, ни
один из них не зовёт `bot.promote_chat_member`/`bot.set_chat_administrator_
custom_title` напрямую.

Контракт порядка блокировок: активная строка `active_titles` того же
`(chat_id, user_id)` блокируется `FOR UPDATE` ПЕРВОЙ (частичный
UNIQUE(chat_id, user_id) WHERE status='active' из миграции 0008 гарантирует
не больше одной active-строки на юзера) — та же форма, что
`duel_service._get_duel_for_update`/`markets_service.place_bet` (row-lock
перед мутацией, T-05-06). Этот `FOR UPDATE` защищает только строку, которая
УЖЕ существует на момент SELECT — он НЕ сериализует гонку, где на момент
SELECT ни одной active-строки ещё нет (05-REVIEW.md CR-01, напр. rental
vs nomination или гонка с регулярным `active_titles_expire`). Поэтому
`grant_title`/`expire_due` дополнительно оборачивают SELECT+мутацию в
SAVEPOINT (`session.begin_nested()`) и ловят `IntegrityError` от частичного
UNIQUE — та же идемпотентная дисциплина, что `economy_service` использует
для денег, только здесь конфликт разрешается перечитыванием состояния и
повтором state machine один раз, а не молчаливым no-op.

Pitfall 1 (verified aiogram 3.27.0): `set_chat_administrator_custom_title`
работает ТОЛЬКО на админах, реально promoted этим ботом — `grant_title`
ВСЕГДА зовёт `promote_chat_member` ПЕРЕД `set_chat_administrator_custom_title`,
без исключений.

Pitfall 3: демот/повторный промоут НЕ сохраняет `custom_title` сам по себе —
`custom_title` это отдельный API-вызов, полностью независимый от
promote_chat_member. `expire_due` при восстановлении подвешенной аренды
ВСЕГДА повторно зовёт `set_chat_administrator_custom_title` со стёртым в
`active_titles.title` значением.

Любой сбой Telegram API (пользователь — реальный, НЕ promoted этим ботом
админ; бот потерял `can_promote_members`) ловится defensively
(TelegramBadRequest/TelegramForbiddenError логируется, не пробрасывается) —
та же дисциплина, что мут проигравшего дуэли
(`test_duel_accept_handler_survives_mute_failure`): состояние `active_titles`
уже согласовано, побочный эффект Telegram-стороны не должен ронять
вызывающий флоу (awards/victim payout, аренда).
"""

from __future__ import annotations

import logging
from datetime import datetime

import emoji
from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.exceptions import TelegramForbiddenError
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import settings
from common.db.session import SessionLocal
from common.models.active_title import ActiveTitle

logger = logging.getLogger(__name__)


class TagError(Exception):
    """Базовое исключение модуля тегов (валидация title и т.п.)."""


# --- Валидация title (T-05-01, V5 Input Validation) --------------------------


def validate_title(title: str) -> str:
    """Обрезает пробелы, отклоняет title длиннее settings.title_max (16) или
    содержащий эмодзи (Telegram custom_title — plain text, ДО любого
    обращения к Bot API, T-05-01: свободный ввод участника при аренде —
    единственное по-настоящему недоверенное место в этой фазе).

    Публичная (WR-04, 05-REVIEW.md) — вызывается не только отсюда, но и
    напрямую из tag_rental_service.rent_title ДО списания денег/Bot API;
    leading-underscore имя неверно сигнализировало бы "internal only", хотя
    у функции есть реальный внешний вызывающий."""
    cleaned = title.strip()
    if not cleaned or len(cleaned) > settings.title_max:
        raise TagError(f"Титул должен быть от 1 до {settings.title_max} символов")
    if emoji.emoji_list(cleaned):
        raise TagError("Титул не может содержать эмодзи")
    return cleaned


# --- Telegram-эффекты (защищённые от сбоя API) -------------------------------


async def _promote_and_set_title(bot: Bot, chat_id: int, user_id: int, title: str) -> None:
    """Pitfall 1: promote_chat_member ВСЕГДА первым, ПОТОМ
    set_chat_administrator_custom_title. Сбой любого из двух вызовов
    ловится, не пробрасывается (Pitfall 2 — реальный не-bot-promoted админ
    или бот без can_promote_members)."""
    try:
        await bot.promote_chat_member(
            chat_id=chat_id,
            user_id=user_id,
            is_anonymous=False,
            can_invite_users=True,  # безобидное право, как в других сервисах
        )
        await bot.set_chat_administrator_custom_title(
            chat_id=chat_id,
            user_id=user_id,
            custom_title=title,
        )
    except (TelegramBadRequest, TelegramForbiddenError):
        logger.warning(
            "tag_service: promote+custom_title не удался для chat_id=%s user_id=%s "
            "(не bot-promoted админ или боту не хватает can_promote_members, "
            "см. docs/botfather-setup.md) — active_titles-строка остаётся на месте",
            chat_id,
            user_id,
        )


async def _demote(bot: Bot, chat_id: int, user_id: int) -> None:
    """Демот = promote_chat_member со всеми правами явно False (снятие тега).
    Defensive-catch — та же дисциплина, что _promote_and_set_title."""
    try:
        await bot.promote_chat_member(
            chat_id=chat_id,
            user_id=user_id,
            is_anonymous=False,
            can_manage_chat=False,
            can_delete_messages=False,
            can_manage_video_chats=False,
            can_restrict_members=False,
            can_promote_members=False,
            can_change_info=False,
            can_invite_users=False,
            can_pin_messages=False,
            can_manage_topics=False,
        )
    except (TelegramBadRequest, TelegramForbiddenError):
        logger.warning(
            "tag_service: демот не удался для chat_id=%s user_id=%s — "
            "active_titles-строка всё равно помечается expired",
            chat_id,
            user_id,
        )


# --- grant_title / clear_title -----------------------------------------------


async def grant_title(
    bot: Bot,
    session: AsyncSession,
    chat_id: int,
    user_id: int,
    title: str,
    source: str,
    expires_at: datetime | None,
    price_paid: int | None = None,
    ref_id: str | None = None,
) -> ActiveTitle:
    """Выдаёт Telegram custom_title. Валидация title ДО любого Bot API
    (T-05-01). Приоритет номинанта над арендатором (D-07/D-10): грант
    'victim' поверх активной 'rental' подвешивает её в 'suspended' (не
    удаляет); грант 'rental' поверх активной 'victim' создаёт новую
    rental-строку сразу 'suspended' (номинант остаётся). Не коммитит —
    транзакцию завершает вызывающий.

    `ref_id` (WR-01, 05-REVIEW.md) — опционально сохраняется на строке как
    есть, БЕЗ идемпотентной проверки здесь (её уже сделал
    economy_service.debit_to_bank в tag_rental_service ДО вызова
    grant_title) — нужен только вызывающему для однозначного поиска СВОЕЙ
    строки на идемпотентном ретрае, вместо recency-эвристики по "последней
    rental-строке юзера". active_titles остаётся под записью ИСКЛЮЧИТЕЛЬНО
    tag_service — вызывающие передают ref_id сюда, а не пишут строку сами."""
    validated_title = validate_title(title)

    # CR-01 (05-REVIEW.md): `SELECT ... FOR UPDATE` только что ниже блокирует
    # строку, которая УЖЕ существует — если в момент нашего SELECT ни одной
    # active-строки нет (или конкурентная транзакция как раз меняет статус
    # строки, на которой мы заблокированы), наш INSERT ниже может столкнуться
    # с частичным UNIQUE(chat_id, user_id) WHERE status='active', который
    # только что зафиксировала выигравшая гонку транзакция. Оборачиваем
    # SELECT+INSERT в SAVEPOINT (форма economy_service: begin_nested() +
    # except IntegrityError) и при конфликте перечитываем актуальное
    # состояние ОДИН раз — тот же дух, что и идемпотентные примитивы
    # economy_service, только тут мы не "уже применено, no-op", а
    # "перезапустить решение state machine с свежими данными".
    for attempt in range(2):
        try:
            async with session.begin_nested():
                existing = (
                    await session.execute(
                        select(ActiveTitle)
                        .where(
                            ActiveTitle.chat_id == chat_id,
                            ActiveTitle.user_id == user_id,
                            ActiveTitle.status == "active",
                        )
                        .with_for_update()
                    )
                ).scalar_one_or_none()

                new_status = "active"
                if existing is not None:
                    if existing.source == "victim" and source == "rental":
                        # Номинант в приоритете — новая аренда рождается подвешенной,
                        # активный тег номинанта не трогаем (D-07).
                        new_status = "suspended"
                    else:
                        # victim поверх active rental, либо любой другой конфликт —
                        # существующая строка подвешивается (не удаляется), новая активна.
                        existing.status = "suspended"
                        new_status = "active"

                row = ActiveTitle(
                    chat_id=chat_id,
                    user_id=user_id,
                    tg_user_id=user_id,  # users.id == Telegram user id в этом проекте
                    title=validated_title,
                    source=source,
                    price_paid=price_paid,
                    expires_at=expires_at,
                    status=new_status,
                    ref_id=ref_id,
                )
                session.add(row)
                await session.flush()
            break
        except IntegrityError:
            if attempt == 1:
                raise
            logger.info(
                "grant_title: гонка за active-строку (chat_id=%s, user_id=%s) — "
                "перечитываем состояние и повторяем один раз",
                chat_id,
                user_id,
            )
            continue

    if new_status == "active":
        await _promote_and_set_title(bot, chat_id, user_id, validated_title)

    return row


async def clear_title(bot: Bot, session: AsyncSession, chat_id: int, user_id: int) -> None:
    """Снимает активный тег: демотит (defensively) и помечает active-строку
    юзера expired. Не восстанавливает подвешенную аренду — это забота
    expire_due (снятие вручную, а не по расписанию, не triggers restore)."""
    await _demote(bot, chat_id, user_id)
    active_rows = (
        await session.execute(
            select(ActiveTitle).where(
                ActiveTitle.chat_id == chat_id,
                ActiveTitle.user_id == user_id,
                ActiveTitle.status == "active",
            )
        )
    ).scalars().all()
    for row in active_rows:
        row.status = "expired"


# --- expire_due (планировщик, D-07 restore) -----------------------------------


async def expire_due(bot: Bot, session: AsyncSession) -> int:
    """Демотит просроченные active-титулы (expires_at <= now) и
    восстанавливает подвешенную аренду того же юзера (suspended->active,
    повторный promote+set_custom_title — Pitfall 3). Возвращает число
    обработанных (демотированных) строк."""
    now = datetime.utcnow()
    due_rows = (
        await session.execute(
            select(ActiveTitle)
            .where(
                ActiveTitle.status == "active",
                ActiveTitle.expires_at.isnot(None),
                ActiveTitle.expires_at <= now,
            )
            .with_for_update()
        )
    ).scalars().all()

    processed = 0
    for row in due_rows:
        await _demote(bot, row.chat_id, row.user_id)
        row.status = "expired"
        processed += 1

        suspended = (
            await session.execute(
                select(ActiveTitle)
                .where(
                    ActiveTitle.chat_id == row.chat_id,
                    ActiveTitle.user_id == row.user_id,
                    ActiveTitle.status == "suspended",
                    ActiveTitle.expires_at.isnot(None),
                    ActiveTitle.expires_at > now,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()

        if suspended is None:
            continue

        # CR-01: тот же класс гонки, что и в grant_title — конкурентный
        # grant_title мог уже выдать новую active-строку этому же
        # (chat_id, user_id) между нашим SELECT due_rows и этим моментом.
        # SAVEPOINT + except IntegrityError не роняет весь тик (и не
        # откатывает уже обработанные строки due_rows): если восстановление
        # конфликтует с частичным UNIQUE(chat_id, user_id) WHERE
        # status='active', grant_title уже согласовал состояние сам (см. его
        # собственный retry) — здесь просто пропускаем это восстановление.
        try:
            async with session.begin_nested():
                suspended.status = "active"
                await session.flush()
        except IntegrityError:
            logger.info(
                "expire_due: восстановление suspended->active для chat_id=%s user_id=%s "
                "столкнулось с конкурентным grant_title, пропускаем",
                row.chat_id,
                row.user_id,
            )
            continue

        # Pitfall 3: титул не переживает demote/promote сам по себе —
        # повторный set_chat_administrator_custom_title обязателен.
        await _promote_and_set_title(bot, suspended.chat_id, suspended.user_id, suspended.title)

    return processed


# --- register_title_expiry (APScheduler, форма markets_service.register_auto_close) --


_EXPIRE_JOB_ID = "active_titles_expire"


def register_title_expiry(scheduler: AsyncIOScheduler, bot: Bot) -> None:
    """Регистрирует фоновый expire_due как interval-job (5 минут), по
    образцу markets_service.register_auto_close: своя сессия, broad-except —
    тик обязан пережить любую ошибку и не уронить планировщик."""

    async def _job() -> None:
        async with SessionLocal() as session:
            try:
                processed = await expire_due(bot, session)
                await session.commit()
                if processed:
                    logger.info("active_titles_expire: обработано просроченных титулов — %s", processed)
            except Exception:  # noqa: BLE001 - job обязан пережить любую ошибку и не уронить планировщик
                logger.exception("active_titles_expire: тик упал")

    scheduler.add_job(
        _job,
        "interval",
        minutes=5,
        coalesce=True,
        max_instances=1,
        misfire_grace_time=120,
        id=_EXPIRE_JOB_ID,
        replace_existing=True,
    )
