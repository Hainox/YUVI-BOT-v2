"""Команды P2P-биржи ювиков (EXCHANGE-01): /exchange /exchange_create
/exchange_claim /exchange_cancel /exchange_confirm, плюс админские
/exchange_admin_cancel /exchange_admin_release для споров. Тонкий хендлер:
парсит вход, зовёт exchange_service, форматирует и отвечает — вся денежная/
статусная логика в сервисе (форма bot/handlers/duel.py/markets.py).

Оплата "того, что хочет продавец" происходит ВНЕ бота — свободный текст
`want_description`, не структурированная цена (см. докстринг
exchange_service.py). Каждый ответ-предупреждение об этом держим коротким
(D-14 общий стиль "не скрывать это в мелком шрифте" — сказано прямо в тексте
создания листинга).

/exchange_admin_cancel /exchange_admin_release — АДМИНСКИЕ команды (форма
bot/handlers/duel.py::unmute_command): ручной гейт admin_service.is_chat_admin
с явным ответом не-админу (не молчаливый ChatAdminFilter) — любой ТЕКУЩИЙ
админ чата может разрешить спор по зависшему claimed-листингу.
"""

from __future__ import annotations

import html

from aiogram import Bot
from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.services import admin_service
from bot.services import economy_service
from bot.services import exchange_service

router = Router()


# --- Разбор входа -----------------------------------------------------------


def _parse_create_args(message: Message) -> tuple[int, str] | None:
    """Парсит `/exchange_create <сумма> <описание того, что хочешь взамен>`."""
    if message.text is None:
        return None
    parts = message.text.split(maxsplit=2)
    if len(parts) < 3 or not parts[1].isdigit():
        return None
    amount = int(parts[1])
    description = parts[2].strip()
    if not description:
        return None
    return amount, description


def _parse_id_arg(message: Message) -> int | None:
    """Парсит `/exchange_claim|cancel|confirm|admin_cancel|admin_release <id>`."""
    if message.text is None:
        return None
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip().isdigit():
        return None
    return int(parts[1].strip())


# --- Чистые форматтеры (юнит-тестируемые без message/session) --------------


def format_listings_list(rows: list[dict]) -> str:
    if not rows:
        return "Открытых листингов на бирже нет."
    lines = ["<b>Биржа — открытые листинги</b>"]
    for row in rows:
        seller = html.escape(row["seller_name"] or str(row["seller_user_id"]))
        want = html.escape(row["want_description"])
        lines.append(f"#{row['id']}: {row['yuvik_amount']} ювиков от {seller} — хочет: {want}")
    lines.append("\nОплата происходит вне бота — договаривайтесь напрямую с продавцом.")
    return "\n".join(lines)


def format_listing_created(listing) -> str:
    want = html.escape(listing.want_description)
    return (
        f"<b>Листинг #{listing.id} создан:</b> {listing.yuvik_amount} ювиков эскроированы.\n"
        f"Хочешь взамен: {want}\n\n"
        "Оплата происходит вне бота, между вами и покупателем — бот удостоверяет "
        "только ювик-сторону сделки. Не подтверждай сделку (/exchange_confirm), "
        "пока реально не получишь оплату."
    )


def format_claim_result(result: dict) -> str:
    if not result["claimed"]:
        return f"Листинг #{result['listing_id']} уже не открыт (статус: {result['status']})."
    return (
        f"Листинг #{result['listing_id']} заклеймлен. Договорись с продавцом об оплате вне "
        "бота — продавец подтвердит сделку сам, когда получит оплату."
    )


def format_cancel_result(result: dict) -> str:
    if result["status"] != exchange_service.STATUS_CANCELLED:
        return f"Листинг #{result['listing_id']} уже {result['status']}, отмена невозможна."
    return f"Листинг #{result['listing_id']} отменён, {result['refunded']} ювиков возвращены."


def format_confirm_result(result: dict) -> str:
    if result["status"] != exchange_service.STATUS_FULFILLED:
        return f"Листинг #{result['listing_id']} сейчас {result['status']}, подтверждение невозможно."
    return f"Листинг #{result['listing_id']} завершён: {result['released']} ювиков переданы покупателю."


# --- Хендлеры -----------------------------------------------------------


@router.message(Command("exchange"))
async def exchange_list_command(message: Message, session: AsyncSession) -> None:
    rows = await exchange_service.get_open_listings(session, message.chat.id)
    await message.answer(format_listings_list(rows), parse_mode="HTML")


@router.message(Command("exchange_create"))
async def exchange_create_command(message: Message, session: AsyncSession) -> None:
    if message.from_user is None:
        return

    parsed = _parse_create_args(message)
    if parsed is None:
        await message.answer(
            "Использование: /exchange_create <сумма> <что хочешь взамен>\n"
            "Например: /exchange_create 10 10 ювиков за подписку на канал"
        )
        return

    amount, description = parsed
    ref_id = f"exchange_create:{message.chat.id}:{message.message_id}"

    try:
        listing = await exchange_service.create_listing(
            session, message.chat.id, message.from_user.id, amount, description, ref_id
        )
    except exchange_service.ExchangeError as exc:
        await message.answer(str(exc))
        return
    except economy_service.InsufficientFunds as exc:
        await message.answer(str(exc))
        return

    await message.answer(format_listing_created(listing), parse_mode="HTML")


@router.message(Command("exchange_claim"))
async def exchange_claim_command(message: Message, session: AsyncSession) -> None:
    if message.from_user is None:
        return

    listing_id = _parse_id_arg(message)
    if listing_id is None:
        await message.answer("Использование: /exchange_claim <id листинга>")
        return

    try:
        result = await exchange_service.claim_listing(
            session, message.chat.id, listing_id, message.from_user.id
        )
    except (exchange_service.ListingNotFound, exchange_service.ExchangeError) as exc:
        await message.answer(str(exc))
        return

    await message.answer(format_claim_result(result))


@router.message(Command("exchange_cancel"))
async def exchange_cancel_command(message: Message, session: AsyncSession) -> None:
    if message.from_user is None:
        return

    listing_id = _parse_id_arg(message)
    if listing_id is None:
        await message.answer("Использование: /exchange_cancel <id листинга>")
        return

    try:
        result = await exchange_service.cancel_listing(
            session, message.chat.id, listing_id, message.from_user.id
        )
    except (exchange_service.ListingNotFound, exchange_service.ExchangeError) as exc:
        await message.answer(str(exc))
        return

    await message.answer(format_cancel_result(result))


@router.message(Command("exchange_confirm"))
async def exchange_confirm_command(message: Message, session: AsyncSession) -> None:
    if message.from_user is None:
        return

    listing_id = _parse_id_arg(message)
    if listing_id is None:
        await message.answer("Использование: /exchange_confirm <id листинга>")
        return

    ref_id = f"exchange_confirm:{message.chat.id}:{message.message_id}"

    try:
        result = await exchange_service.confirm_fulfillment(
            session, message.chat.id, listing_id, message.from_user.id, ref_id
        )
    except (exchange_service.ListingNotFound, exchange_service.ExchangeError) as exc:
        await message.answer(str(exc))
        return

    await message.answer(format_confirm_result(result))


@router.message(Command("exchange_admin_cancel"))
async def exchange_admin_cancel_command(message: Message, session: AsyncSession, bot: Bot) -> None:
    """Спор: принудительная отмена (рефанд продавцу) — только текущий
    (live-проверка) админ чата, явный отказ не-админу."""
    if message.from_user is None:
        return
    if not await admin_service.is_chat_admin(bot, message.chat.id, message.from_user.id):
        await message.reply("Только администратор чата может принудительно отменить листинг.")
        return

    listing_id = _parse_id_arg(message)
    if listing_id is None:
        await message.answer("Использование: /exchange_admin_cancel <id листинга>")
        return

    try:
        result = await exchange_service.admin_force_cancel(session, message.chat.id, listing_id)
    except exchange_service.ListingNotFound as exc:
        await message.answer(str(exc))
        return

    await message.answer(format_cancel_result(result))


@router.message(Command("exchange_admin_release"))
async def exchange_admin_release_command(message: Message, session: AsyncSession, bot: Bot) -> None:
    """Спор: принудительный релиз эскроу покупателю — только текущий
    (live-проверка) админ чата, явный отказ не-админу."""
    if message.from_user is None:
        return
    if not await admin_service.is_chat_admin(bot, message.chat.id, message.from_user.id):
        await message.reply("Только администратор чата может принудительно завершить листинг.")
        return

    listing_id = _parse_id_arg(message)
    if listing_id is None:
        await message.answer("Использование: /exchange_admin_release <id листинга>")
        return

    try:
        result = await exchange_service.admin_force_release(session, message.chat.id, listing_id)
    except (exchange_service.ListingNotFound, exchange_service.ExchangeError) as exc:
        await message.answer(str(exc))
        return

    await message.answer(format_confirm_result(result))
