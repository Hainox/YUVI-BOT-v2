"""Рынок аренды тегов (TAG-02) — любой участник арендует свой Telegram
custom_title за ювики на 1/3/7 дней (цена settings.tag_rent_per_day/день,
D-07/D-08). Деньги — ТОЛЬКО через economy_service (debit_to_bank, идемпотентно
через ref_id); выдача/приоритет/восстановление тега — ТОЛЬКО через
tag_service.grant_title(source='rental')/clear_title — этот модуль сам
НИКОГДА не пишет user_balance/chat_bank/active_titles напрямую и не зовёт
Bot API напрямую (05-03: tag_service — единственный владелец
custom_title/active_titles).

Свободный title (пользовательский ввод) валидируется через
tag_service._validate_title ДО списания денег и ДО любого обращения к Bot
API (T-05-01) — единственная untrusted-free-text-точка этой фазы.

Идемпотентность: ref_id аренды — f"tag_rent:{chat_id}:{message_id}"
(назначается вызывающим хендлером). Повтор того же ref_id (ретрай апдейта
Telegram) ловится через economy_service.debit_to_bank — деньги не
списываются повторно, grant_title (а значит и Bot API) не вызывается
повторно; вызывающий получает уже созданную ранее rental-строку.
"""

from __future__ import annotations

from datetime import datetime
from datetime import timedelta

from aiogram import Bot
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import settings
from bot.services import economy_service
from bot.services import tag_service
from common.models.active_title import ActiveTitle


class TagRentalError(Exception):
    """Невалидный срок аренды (days вне settings.tag_rent_allowed_days)."""


def _allowed_days() -> frozenset[int]:
    """Разбирает settings.tag_rent_allowed_days ("1,3,7") в frozenset[int]."""
    return frozenset(int(chunk) for chunk in settings.tag_rent_allowed_days.split(",") if chunk.strip())


def _price(days: int) -> int:
    """Цена аренды на `days` дней — settings.tag_rent_per_day * days."""
    return settings.tag_rent_per_day * days


async def rent_title(
    session: AsyncSession,
    chat_id: int,
    user_id: int,
    title: str,
    days: int,
    ref_id: str,
    bot: Bot,
) -> ActiveTitle:
    """Аренда custom_title: days валидируется ПЕРВЫМ (дешёвая проверка без
    похода к деньгам/API), затем title через tag_service._validate_title
    (T-05-01 — ДО денег и ДО Bot API), затем идемпотентное списание в банк
    чата (economy_service.debit_to_bank), и только после успешного списания —
    выдача через tag_service.grant_title(source='rental'); приоритет
    номинанта над арендатором (D-07) целиком реализован в grant_title'овой
    state machine, здесь не дублируется.

    Повтор ref_id (debit_to_bank вернул False) — идемпотентный no-op: деньги
    не списываются и grant_title не вызывается повторно, возвращается
    последняя rental-строка вызывающего, созданная первым (успешным)
    вызовом. Поднимает TagRentalError на невалидный days, tag_service.TagError
    на невалидный title, economy_service.InsufficientFunds на нехватку
    средств — во всех трёх случаях титул не выдаётся и деньги не двигаются.
    Не коммитит — транзакцию завершает вызывающий (форма economy_service).
    """
    if days not in _allowed_days():
        allowed = ", ".join(str(d) for d in sorted(_allowed_days()))
        raise TagRentalError(f"Аренда доступна только на {allowed} дн.")

    validated_title = tag_service._validate_title(title)  # T-05-01, ДО денег/API

    price = _price(days)
    charged = await economy_service.debit_to_bank(
        session, chat_id, user_id, price, kind="tag_rent", ref_id=ref_id
    )
    if not charged:
        existing = (
            await session.execute(
                select(ActiveTitle)
                .where(
                    ActiveTitle.chat_id == chat_id,
                    ActiveTitle.user_id == user_id,
                    ActiveTitle.source == "rental",
                )
                .order_by(ActiveTitle.id.desc())
            )
        ).scalars().first()
        if existing is None:
            # Тот же ref_id уже применён к деньгам, но rental-строки нет —
            # структурно не должно случаться (grant_title всегда идёт сразу
            # после успешного debit_to_bank в том же вызове), но не молчим.
            raise TagRentalError("Аренда с этим ref_id уже обработана, но запись не найдена")
        return existing

    expires_at = datetime.utcnow() + timedelta(days=days)
    return await tag_service.grant_title(
        bot,
        session,
        chat_id,
        user_id,
        validated_title,
        source="rental",
        expires_at=expires_at,
        price_paid=price,
    )


async def cancel_rental(session: AsyncSession, chat_id: int, user_id: int, bot: Bot) -> bool:
    """Отмена аренды (действует только на строку вызывающего, V4): FOR UPDATE
    активной/подвешенной 'rental'-строки (chat_id, user_id). Нет такой строки
    → False (идемпотентный no-op на повторный вызов). Иначе — status=
    'cancelled'; если строка была активна, снимает реальный Telegram-тег
    через tag_service.clear_title (defensive demote). Рефанд не
    предусмотрен (D-07). Не коммитит — транзакцию завершает вызывающий.

    `.scalars().first()` (не `scalar_one_or_none`) — намеренно: у пользователя
    может накопиться больше одной 'rental'-строки в статусе suspended (если
    он арендовал повторно поверх собственной подвешенной аренды), частичный
    UNIQUE гарантирует уникальность только для status='active'; берём
    последнюю по id, чтобы не падать на MultipleResultsFound."""
    row = (
        await session.execute(
            select(ActiveTitle)
            .where(
                ActiveTitle.chat_id == chat_id,
                ActiveTitle.user_id == user_id,
                ActiveTitle.source == "rental",
                ActiveTitle.status.in_(("active", "suspended")),
            )
            .order_by(ActiveTitle.id.desc())
            .with_for_update()
        )
    ).scalars().first()

    if row is None:
        return False

    was_active = row.status == "active"
    row.status = "cancelled"

    if was_active:
        await tag_service.clear_title(bot, session, chat_id, user_id)

    return True
