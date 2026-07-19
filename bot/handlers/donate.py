"""/donate N — донат Telegram Stars → ювики (STARS-01, D-09/D-11/D-12).

Тонкий хендлер: `cmd_donate` парсит N и шлёт XTR-инвойс, `on_pre_checkout`
только ack'ает (Pitfall 2, 10s SLA — ни одного обращения к БД/сервисам ДО
ack), `on_successful_payment` зовёт `stars_service.credit_from_payment`
(идемпотентно по `telegram_payment_charge_id`) и постит публичное
благодарственное сообщение (D-11) — но только при первом (не replay)
начислении. Router подхватывается `bot/main.py::_discover_routers`
автоматически.
"""

from __future__ import annotations

import html
import logging

from aiogram import F
from aiogram import Router
from aiogram.filters import Command
from aiogram.filters import CommandObject
from aiogram.types import Message
from aiogram.types import PreCheckoutQuery
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import settings
from bot.services import stars_service

router = Router()
logger = logging.getLogger(__name__)


def _parse_positive_int(arg: str | None) -> int | None:
    """Строго положительное целое (D-12: минимум 1⭐, без верхнего предела —
    его естественно ограничивает сам Telegram на экране оплаты)."""
    if arg is None:
        return None
    arg = arg.strip()
    if not arg.lstrip("-").isdigit():
        return None
    value = int(arg)
    return value if value > 0 else None


@router.message(Command("donate"))
async def cmd_donate(message: Message, command: CommandObject) -> None:
    if message.from_user is None:
        return

    stars = _parse_positive_int(command.args)
    if stars is None:
        await message.reply("Использование: /donate <количество звёзд> (минимум 1)")
        return

    await message.bot.send_invoice(
        chat_id=message.chat.id,
        **stars_service.build_invoice_kwargs(stars, message.from_user.id, settings.stars_to_juvik_rate),
    )


@router.pre_checkout_query()
async def on_pre_checkout(pre_checkout_query: PreCheckoutQuery) -> None:
    """Только ack — Pitfall 2, 10s SLA. Никаких обращений к БД/сервисам."""
    await pre_checkout_query.answer(ok=True)


@router.message(F.successful_payment)
async def on_successful_payment(message: Message, session: AsyncSession) -> None:
    if message.from_user is None:
        return

    payment = message.successful_payment
    charge_id = payment.telegram_payment_charge_id
    stars = payment.total_amount

    credited = await stars_service.credit_from_payment(
        session, message.chat.id, message.from_user.id, stars, charge_id
    )
    await session.commit()
    if not credited:
        # Повтор апдейта (реконнект polling) — идемпотентный no-op, повторный
        # пост благодарности не отправляется.
        return

    juviks = stars * settings.stars_to_juvik_rate
    name = html.escape(message.from_user.first_name or str(message.from_user.id))
    await message.answer(f"🌟 {name} задонатил {stars}⭐ (+{juviks} ювиков)!")
