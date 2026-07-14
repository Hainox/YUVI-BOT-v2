"""Команды рынков ставок (BET-01): /market_create /bet /markets /market
/portfolio. Тонкий хендлер: парсит вход, зовёт markets_service, форматирует
и отвечает — вся логика жизненного цикла рынка в сервисе.

Вопрос/варианты рынка — НОВЫЙ пользовательский HTML-surface этой фазы
(T-03-13/Pitfall 6): каждый format_* прогоняет question/label через
html.escape перед вставкой в parse_mode="HTML"-ответ, аналогично уже
принятой конвенции stats.py/economy.py.

/market_resolve и /market_cancel (D-01/D-02, ChatAdminFilter) — вне scope
этого плана, добавляются планом 03-05.
"""

from __future__ import annotations

import html

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.services import economy_service
from bot.services import markets_service

router = Router()


# --- Разбор входа -----------------------------------------------------------


def _parse_market_create_args(message: Message) -> tuple[str, str, list[str]] | None:
    """Парсит `/market_create <длительность> <вопрос> | <в1> | <в2> [...]`.

    Длительность — ПЕРВЫЙ токен без пробелов (например "7d" или
    "7d|12h|90m"); остаток делится по "|" на вопрос + варианты.
    """
    if message.text is None:
        return None
    command_parts = message.text.split(maxsplit=1)
    if len(command_parts) < 2:
        return None

    duration_parts = command_parts[1].split(maxsplit=1)
    if len(duration_parts) < 2:
        return None
    duration_raw, body = duration_parts

    segments = [segment.strip() for segment in body.split("|")]
    if not segments or not segments[0]:
        return None
    question, *options = segments
    return duration_raw, question, options


def _parse_bet_args(message: Message) -> tuple[int, int, int] | None:
    """Парсит `/bet <id рынка> <номер варианта> <сумма>` — три целых числа."""
    if message.text is None:
        return None
    tokens = message.text.split()[1:]
    if len(tokens) != 3 or not all(token.lstrip("-").isdigit() for token in tokens):
        return None
    market_id, option_position, amount = (int(token) for token in tokens)
    return market_id, option_position, amount


def _parse_market_id_arg(message: Message) -> int | None:
    """Парсит `/market <id>`."""
    if message.text is None:
        return None
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip().isdigit():
        return None
    return int(parts[1].strip())


# --- Чистые форматтеры (юнит-тестируемые без message/session) --------------


def format_market_created(detail: dict) -> str:
    question = html.escape(detail["question"])
    options_lines = "\n".join(
        f"{option['position']}) {html.escape(option['label'])}" for option in detail["options"]
    )
    closes_at = detail["closes_at"].strftime("%d.%m.%Y %H:%M")
    return (
        f"<b>Рынок #{detail['id']} создан:</b> {question}\n"
        f"Варианты:\n{options_lines}\n"
        f"Ставки принимаются до {closes_at} (UTC)."
    )


def format_markets_list(rows: list[dict]) -> str:
    if not rows:
        return "Открытых рынков нет."
    lines = ["<b>Открытые рынки</b>"]
    for row in rows:
        question = html.escape(row["question"])
        closes_at = row["closes_at"].strftime("%d.%m.%Y %H:%M")
        lines.append(f"#{row['id']}: {question} (закрытие {closes_at})")
    return "\n".join(lines)


def format_market_detail(detail: dict) -> str:
    question = html.escape(detail["question"])
    lines = [f"<b>Рынок #{detail['id']}</b>: {question}", f"Статус: {detail['status']}"]
    for option in detail["options"]:
        label = html.escape(option["label"])
        lines.append(f"{option['position']}) {label} — {option['pool']} ({option['share_pct']}%)")
    lines.append(f"Суммарный пул: {detail['total_pool']}")
    closes_at = detail["closes_at"].strftime("%d.%m.%Y %H:%M")
    lines.append(f"Закрытие: {closes_at} (UTC)")
    return "\n".join(lines)


def format_portfolio(rows: list[dict]) -> str:
    if not rows:
        return "У вас нет ставок."
    lines = ["<b>Мои ставки</b>"]
    for row in rows:
        question = html.escape(row["question"])
        label = html.escape(row["option_label"])
        if row["market_status"] == "resolved":
            outcome = f"выплата {row['payout'] or 0}"
        elif row["refunded"]:
            outcome = "рефанд"
        else:
            outcome = "в игре"
        lines.append(
            f"#{row['market_id']} {question} — {label}, ставка {row['amount']} ({outcome})"
        )
    return "\n".join(lines)


# --- Хендлеры ----------------------------------------------------------------


@router.message(Command("market_create"))
async def market_create_command(message: Message, session: AsyncSession) -> None:
    if message.from_user is None:
        return

    parsed = _parse_market_create_args(message)
    if parsed is None:
        await message.answer(
            "Использование: /market_create <длительность> <вопрос> | <вариант1> | <вариант2> [...]\n"
            "Например: /market_create 7d Кто выиграет турнир? | Команда А | Команда Б"
        )
        return

    duration_raw, question, options = parsed
    ref_id = f"market_create:{message.chat.id}:{message.message_id}"

    try:
        market = await markets_service.create_market(
            session, message.chat.id, message.from_user.id, question, options, duration_raw, ref_id
        )
    except (markets_service.DurationError, markets_service.InvalidMarketArg) as exc:
        await message.answer(
            f"{exc}\n\n"
            "Использование: /market_create <длительность> <вопрос> | <вариант1> | <вариант2> [...]"
        )
        return
    except economy_service.InsufficientFunds as exc:
        await message.answer(str(exc))
        return
    except markets_service.DuplicateRequest:
        await message.answer("Этот запрос на создание рынка уже обработан.")
        return

    detail = await markets_service.get_market_detail(session, message.chat.id, market.id)
    await message.answer(format_market_created(detail), parse_mode="HTML")


@router.message(Command("bet"))
async def bet_command(message: Message, session: AsyncSession) -> None:
    if message.from_user is None:
        return

    parsed = _parse_bet_args(message)
    if parsed is None:
        await message.answer("Использование: /bet <id рынка> <номер варианта> <сумма>")
        return

    market_id, option_position, amount = parsed
    ref_id = f"bet:{message.chat.id}:{message.message_id}"

    try:
        bet = await markets_service.place_bet(
            session, message.chat.id, market_id, message.from_user.id, option_position, amount, ref_id
        )
    except (
        markets_service.MarketNotFound,
        markets_service.MarketClosed,
        markets_service.InvalidMarketArg,
    ) as exc:
        await message.answer(str(exc))
        return
    except economy_service.InsufficientFunds as exc:
        await message.answer(str(exc))
        return

    if bet is None:
        await message.answer("Эта ставка уже была принята ранее.")
        return

    detail = await markets_service.get_market_detail(session, message.chat.id, market_id)
    option_label = next(
        option["label"] for option in detail["options"] if option["position"] == option_position
    )
    await message.answer(
        f"Ставка принята: {amount} ювиков на «{html.escape(option_label)}» в рынке #{market_id}.",
        parse_mode="HTML",
    )


@router.message(Command("markets"))
async def markets_command(message: Message, session: AsyncSession) -> None:
    rows = await markets_service.get_open_markets(session, message.chat.id)
    await message.answer(format_markets_list(rows), parse_mode="HTML")


@router.message(Command("market"))
async def market_command(message: Message, session: AsyncSession) -> None:
    market_id = _parse_market_id_arg(message)
    if market_id is None:
        await message.answer("Использование: /market <id рынка>")
        return

    try:
        detail = await markets_service.get_market_detail(session, message.chat.id, market_id)
    except markets_service.MarketNotFound:
        await message.answer("Рынок не найден.")
        return

    await message.answer(format_market_detail(detail), parse_mode="HTML")


@router.message(Command("portfolio"))
async def portfolio_command(message: Message, session: AsyncSession) -> None:
    if message.from_user is None:
        return

    rows = await markets_service.get_user_portfolio(session, message.chat.id, message.from_user.id)
    await message.answer(format_portfolio(rows), parse_mode="HTML")
