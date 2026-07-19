"""Рынок аренды тегов (TAG-02, D-07/D-08): /tag_rent /tag_cancel. Тонкий
хендлер — вся валидация/оплата/выдача в tag_rental_service, этот модуль сам
никогда не зовёт economy_service/tag_service напрямую для мутации денег или
Telegram custom_title. Действуют ТОЛЬКО на вызывающего
(message.from_user.id, V4, T-05-04) — @arg-цель здесь не принимается вовсе,
в отличие от /transfer.
"""

from __future__ import annotations

from aiogram import Bot
from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.services import economy_service
from bot.services import tag_rental_service
from bot.services import tag_service

router = Router()


def _parse_rent_args(message: Message) -> tuple[int | None, str | None]:
    """Парсит `/tag_rent <дни> <титул>`. days — первый токен, только если он
    состоит целиком из цифр (иначе None); title — остаток строки как есть
    (обрезка пробелов и валидация длины/эмодзи — в tag_service._validate_title,
    внутри tag_rental_service.rent_title, T-05-01)."""
    if message.text is None:
        return None, None
    tokens = message.text.split(maxsplit=2)[1:]
    if len(tokens) < 2:
        return None, None
    days_token, title = tokens[0], tokens[1]
    days = int(days_token) if days_token.isdigit() else None
    return days, title


@router.message(Command("tag_rent"))
async def tag_rent_command(message: Message, session: AsyncSession, bot: Bot) -> None:
    if message.from_user is None:
        return

    days, title = _parse_rent_args(message)
    if days is None or not title:
        await message.answer(
            "Формат: /tag_rent <дни> <титул>, например: /tag_rent 3 Босс."
        )
        return

    ref_id = f"tag_rent:{message.chat.id}:{message.message_id}"

    try:
        row = await tag_rental_service.rent_title(
            session, message.chat.id, message.from_user.id, title, days, ref_id, bot
        )
    except (
        tag_rental_service.TagRentalError,
        tag_service.TagError,
        economy_service.InsufficientFunds,
    ) as exc:
        await message.answer(str(exc))
        return

    await session.commit()

    await message.answer(
        f"Тег «{row.title}» арендован на {days} дн. Списано {row.price_paid} ювиков."
    )


@router.message(Command("tag_cancel"))
async def tag_cancel_command(message: Message, session: AsyncSession, bot: Bot) -> None:
    if message.from_user is None:
        return

    cancelled = await tag_rental_service.cancel_rental(
        session, message.chat.id, message.from_user.id, bot
    )
    await session.commit()

    if cancelled:
        await message.answer("Аренда тега отменена.")
    else:
        await message.answer("Активной аренды тега нет.")
