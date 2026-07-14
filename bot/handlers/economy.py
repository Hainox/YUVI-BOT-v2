"""Экономические команды (ECON-02): /balance /transfer /leaderboard /economy
/rules. Тонкий хендлер: парсит вход, зовёт economy_service, форматирует и
отвечает — вся денежная логика в сервисе (единственный модуль, которому
разрешено писать user_balance/chat_bank/economy_tx).

Все пользовательские имена (first_name/username) прогоняются через
html.escape перед вставкой в HTML-ответ (ASVS V5, конвенция stats.py/card.py).
"""

from __future__ import annotations

import html
import math

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import settings
from bot.services import economy_service
from common.models.user import User

router = Router()


# --- Разбор входа -------------------------------------------------------


def _display_name(message: Message) -> str:
    """Отображаемое имя вызывающего, безопасное для HTML-вывода."""
    user = message.from_user
    assert user is not None
    return html.escape(user.first_name or str(user.id))


def _parse_transfer_args(message: Message) -> tuple[str | None, int | None]:
    """Парсит `/transfer <@username|id> <сумма>` либо (в ответ на сообщение)
    `/transfer <сумма>`. Сумма — последний токен, если он состоит только из
    цифр (иначе None). target_arg — первый токен, только если токенов >= 2
    (иначе цель резолвится через reply/mention, а не текстовый аргумент)."""
    if message.text is None:
        return None, None
    tokens = message.text.split()[1:]
    if not tokens:
        return None, None
    amount_token = tokens[-1]
    amount = int(amount_token) if amount_token.isdigit() else None
    target_arg = tokens[0] if len(tokens) >= 2 else None
    return target_arg, amount


async def _resolve_by_username_or_id(session: AsyncSession, arg: str) -> tuple[int, str] | None:
    """Резолвит `@username` или числовой id через таблицу users (аналог card.py)."""
    if arg.startswith("@"):
        stmt = select(User.id, User.first_name).where(User.username == arg[1:])
    elif arg.lstrip("-").isdigit():
        stmt = select(User.id, User.first_name).where(User.id == int(arg))
    else:
        return None

    row = (await session.execute(stmt)).first()
    if row is None:
        return None
    return row.id, row.first_name or str(row.id)


async def _resolve_transfer_target(
    message: Message, session: AsyncSession, target_arg: str | None
) -> tuple[int, str] | None:
    """Резолв получателя `/transfer`: reply > text_mention entity > @username/id-аргумент."""
    if message.reply_to_message is not None and message.reply_to_message.from_user is not None:
        user = message.reply_to_message.from_user
        return user.id, user.first_name or str(user.id)

    if message.entities:
        for entity in message.entities:
            if entity.type == "text_mention" and entity.user is not None:
                user = entity.user
                return user.id, user.first_name or str(user.id)

    if target_arg is not None:
        return await _resolve_by_username_or_id(session, target_arg)

    return None


# --- Чистые форматтеры (юнит-тестируемые без message/session) ------------


def plural_yuviki(n: int) -> str:
    """Склонение слова «ювик» по правилам русского числительного."""
    n_abs = abs(n)
    if n_abs % 100 in (11, 12, 13, 14):
        return "ювиков"
    last_digit = n_abs % 10
    if last_digit == 1:
        return "ювик"
    if last_digit in (2, 3, 4):
        return "ювика"
    return "ювиков"


def format_balance(display_name: str, balance: int) -> str:
    return f"{display_name}, ваш баланс: <b>{balance} {plural_yuviki(balance)}</b>."


def format_transfer_success(amount: int, to_name: str, fee: int) -> str:
    return (
        f"Перевод выполнен: {amount} {plural_yuviki(amount)} → {to_name}, "
        f"комиссия {fee} {plural_yuviki(fee)} ушла в банк чата."
    )


def format_leaderboard(rows: list[dict]) -> str:
    if not rows:
        return "<b>Топ по балансу</b>\nПока никто не завёл кошелёк ювиков."
    lines = ["<b>Топ по балансу</b>"]
    for i, row in enumerate(rows, start=1):
        name = html.escape(row["first_name"] or str(row["user_id"]))
        lines.append(f"{i}. {name} — {row['balance']} {plural_yuviki(row['balance'])}")
    return "\n".join(lines)


def format_chat_summary(summary: dict) -> str:
    bank = summary["bank_balance"]
    circulation = summary["total_in_circulation"]
    return (
        "<b>Экономика чата</b>\n"
        f"Банк чата: {bank} {plural_yuviki(bank)}\n"
        f"В обороте у участников: {circulation} {plural_yuviki(circulation)}\n"
        f"Открытых рынков ставок: {summary['open_markets_count']}"
    )


def format_rules() -> str:
    transfer_fee_pct = round(settings.transfer_fee_pct * 100)
    resolution_fee_pct = round(settings.market_resolution_fee_pct * 100)
    lines = [
        "<b>Правила экономики «Ювики»</b>",
        f"• Стартовый бонус новичку: {settings.economy_start_bonus} ювиков (один раз, "
        "при первом обращении к экономике).",
        f"• Комиссия перевода /transfer: {transfer_fee_pct}% от суммы (минимум 1 ювик), "
        "уходит в банк чата.",
        f"• Комиссия создания рынка ставок /market_create: {settings.market_creation_fee} "
        "ювиков в банк чата.",
        f"• Комиссия резолюции рынка: {resolution_fee_pct}% от общего пула ставок, "
        "уходит в банк чата.",
        "• Выплата победителям parimutuel-рынка — пропорционально сумме ставки от "
        "оставшегося после комиссии пула.",
    ]
    return "\n".join(lines)


# --- Хендлеры -------------------------------------------------------------


@router.message(Command("balance"))
async def balance_command(message: Message, session: AsyncSession) -> None:
    if message.from_user is None:
        return

    balance = await economy_service.get_balance(session, message.chat.id, message.from_user.id)
    text = format_balance(_display_name(message), balance)

    await message.answer(text, parse_mode="HTML")


@router.message(Command("transfer"))
async def transfer_command(message: Message, session: AsyncSession) -> None:
    if message.from_user is None:
        return

    target_arg, amount = _parse_transfer_args(message)
    target = await _resolve_transfer_target(message, session, target_arg)

    if target is None:
        await message.answer(
            "Ответьте на сообщение получателя командой /transfer <сумма> "
            "или используйте /transfer @username <сумма>."
        )
        return

    if amount is None:
        await message.answer("Укажите сумму перевода числом, например: /transfer @username 100.")
        return

    to_user_id, to_raw_name = target
    ref_id = f"transfer:{message.chat.id}:{message.message_id}"

    try:
        await economy_service.transfer_with_fee(
            session, message.chat.id, message.from_user.id, to_user_id, amount, ref_id
        )
    except (economy_service.InvalidArgument, economy_service.InsufficientFunds) as exc:
        await message.answer(str(exc))
        return

    fee = max(1, math.ceil(amount * settings.transfer_fee_pct))
    text = format_transfer_success(amount, html.escape(to_raw_name), fee)

    await message.answer(text, parse_mode="HTML")


@router.message(Command("leaderboard"))
async def leaderboard_command(message: Message, session: AsyncSession) -> None:
    rows = await economy_service.get_leaderboard(session, message.chat.id, limit=10)
    text = format_leaderboard(rows)

    await message.answer(text, parse_mode="HTML")


@router.message(Command("economy"))
async def economy_command(message: Message, session: AsyncSession) -> None:
    summary = await economy_service.get_chat_summary(session, message.chat.id)
    text = format_chat_summary(summary)

    await message.answer(text, parse_mode="HTML")


@router.message(Command("rules"))
async def rules_command(message: Message) -> None:
    await message.answer(format_rules(), parse_mode="HTML")
