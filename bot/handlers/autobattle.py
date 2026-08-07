"""Команды автобитв (GACHA-06): /autobattle /autobattle_accept
/autobattle_decline /autobattle_cancel. Тонкий хендлер — форма
bot/handlers/duel.py (читай его докстринг за разбором конвенций): парсит
вход, зовёт autobattle_service, форматирует и отвечает, деньги/статус целиком
внутри сервиса.

В отличие от Duel здесь нет мут-эффекта проигравшего (autobattle_service не
резолвит `mute_seconds` — исход только двигает деньги), поэтому хендлер
проще: никакого restrict_chat_member/стикеров/scheduler.

Кнопки принятия/отклонения — та же идея, что у Duel (`_autobattle_keyboard`
на сообщении вызова, callback'и зовут те же autobattle_service.
accept_autobattle/decline_autobattle, что и текстовые команды, IDOR-защита
уже внутри сервиса через `opponent_id`-аргумент).
"""

from __future__ import annotations

import html
import logging

from aiogram import F
from aiogram import Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery
from aiogram.types import InlineKeyboardButton
from aiogram.types import InlineKeyboardMarkup
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.services import autobattle_service
from bot.services import economy_service
from bot.services.target_resolution_service import resolve_target

logger = logging.getLogger(__name__)

router = Router()


def _display_name(message: Message) -> str:
    user = message.from_user
    assert user is not None
    return html.escape(user.first_name or str(user.id))


def _parse_autobattle_args(message: Message) -> tuple[str | None, int | None]:
    """Парсит `/autobattle <@user|reply> <ставка>` либо (в ответ на
    сообщение) `/autobattle <ставка>` — форма duel.py::_parse_duel_args."""
    if message.text is None:
        return None, None
    tokens = message.text.split()[1:]
    if not tokens:
        return None, None
    amount_token = tokens[-1]
    amount = int(amount_token) if amount_token.isdigit() else None
    target_arg = tokens[0] if len(tokens) >= 2 else None
    return target_arg, amount


def _parse_id_arg(message: Message) -> int | None:
    """Парсит `/autobattle_accept|/autobattle_decline|/autobattle_cancel <id>`."""
    if message.text is None:
        return None
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip().isdigit():
        return None
    return int(parts[1].strip())


def _autobattle_keyboard(autobattle_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Принять", callback_data=f"autobattle_accept:{autobattle_id}"
                ),
                InlineKeyboardButton(
                    text="❌ Отклонить", callback_data=f"autobattle_decline:{autobattle_id}"
                ),
            ]
        ]
    )


def _result_text(autobattle_id: int, result: dict) -> str:
    return (
        f"<b>Автобитва #{autobattle_id} завершена:</b> сила {result['challenger_power']} "
        f"против {result['opponent_power']} — победитель забирает {result['pot']} ювиков "
        f"(комиссия в банк: {result['fee']})."
    )


# --- Хендлеры -----------------------------------------------------------


@router.message(Command("autobattle"))
async def autobattle_command(message: Message, session: AsyncSession) -> None:
    if message.from_user is None:
        return

    target_arg, amount = _parse_autobattle_args(message)
    if amount is None:
        await message.answer(
            "Использование: /autobattle <ставка> (ответом на сообщение соперника) "
            "или /autobattle @username <ставка>."
        )
        return

    target = await resolve_target(message, session, target_arg)
    if target is None:
        await message.answer(
            "Ответьте на сообщение соперника командой /autobattle <ставка> "
            "или используйте /autobattle @username <ставка>."
        )
        return

    opponent_id, opponent_name = target
    if opponent_id == message.from_user.id:
        await message.answer("Нельзя вызвать на автобитву самого себя.")
        return

    ref_id = f"autobattle:{message.chat.id}:{message.message_id}"
    try:
        battle = await autobattle_service.create_autobattle(
            session, message.chat.id, message.from_user.id, opponent_id, amount, ref_id
        )
    except autobattle_service.AutoBattleError as exc:
        await message.answer(str(exc))
        return
    except economy_service.InsufficientFunds as exc:
        await message.answer(str(exc))
        return

    await message.answer(
        f"<b>Автобитва #{battle.id}:</b> {_display_name(message)} вызывает "
        f"{html.escape(opponent_name)} на ставку {amount} ювиков — исход решит сила "
        f"гача-коллекции каждой стороны.",
        parse_mode="HTML",
        reply_markup=_autobattle_keyboard(battle.id),
    )


@router.message(Command("autobattle_accept"))
async def autobattle_accept_command(message: Message, session: AsyncSession) -> None:
    if message.from_user is None:
        return

    autobattle_id = _parse_id_arg(message)
    if autobattle_id is None:
        await message.answer("Использование: /autobattle_accept <id автобитвы>")
        return

    ref_id = f"autobattle_accept:{message.chat.id}:{message.message_id}"
    try:
        result = await autobattle_service.accept_autobattle(
            session, message.chat.id, autobattle_id, message.from_user.id, ref_id
        )
    except (autobattle_service.AutoBattleNotFound, autobattle_service.AutoBattleError) as exc:
        await message.answer(str(exc))
        return
    except economy_service.InsufficientFunds as exc:
        await message.answer(str(exc))
        return

    if result["status"] != "resolved":
        await message.answer(f"Автобитва #{autobattle_id} уже {result['status']}.")
        return

    await message.answer(_result_text(autobattle_id, result), parse_mode="HTML")


@router.message(Command("autobattle_decline"))
async def autobattle_decline_command(message: Message, session: AsyncSession) -> None:
    if message.from_user is None:
        return

    autobattle_id = _parse_id_arg(message)
    if autobattle_id is None:
        await message.answer("Использование: /autobattle_decline <id автобитвы>")
        return

    try:
        result = await autobattle_service.decline_autobattle(
            session, message.chat.id, autobattle_id, message.from_user.id
        )
    except (autobattle_service.AutoBattleNotFound, autobattle_service.AutoBattleError) as exc:
        await message.answer(str(exc))
        return

    if result["status"] != "declined":
        await message.answer(f"Автобитва #{autobattle_id} уже {result['status']}.")
        return
    await message.answer(f"Автобитва #{autobattle_id} отклонена, ставка возвращена.")


@router.message(Command("autobattle_cancel"))
async def autobattle_cancel_command(message: Message, session: AsyncSession) -> None:
    if message.from_user is None:
        return

    autobattle_id = _parse_id_arg(message)
    if autobattle_id is None:
        await message.answer("Использование: /autobattle_cancel <id автобитвы>")
        return

    try:
        result = await autobattle_service.cancel_autobattle(
            session, message.chat.id, autobattle_id, message.from_user.id
        )
    except (autobattle_service.AutoBattleNotFound, autobattle_service.AutoBattleError) as exc:
        await message.answer(str(exc))
        return

    if result["status"] != "cancelled":
        await message.answer(f"Автобитва #{autobattle_id} уже {result['status']}.")
        return
    await message.answer(f"Автобитва #{autobattle_id} отменена, ставка возвращена.")


@router.callback_query(F.data.startswith("autobattle_accept:"))
async def autobattle_accept_callback(callback: CallbackQuery, session: AsyncSession) -> None:
    """Кнопка «✅ Принять» на сообщении вызова — тот же `accept_autobattle`,
    что `/autobattle_accept`, IDOR-гард уже внутри сервиса."""
    if callback.from_user is None or callback.message is None or callback.data is None:
        return
    try:
        autobattle_id = int(callback.data.split(":", 1)[1])
    except ValueError:
        await callback.answer("Некорректная автобитва.", show_alert=True)
        return

    ref_id = f"autobattle_accept:{callback.message.chat.id}:{callback.id}"
    try:
        result = await autobattle_service.accept_autobattle(
            session, callback.message.chat.id, autobattle_id, callback.from_user.id, ref_id
        )
    except (autobattle_service.AutoBattleNotFound, autobattle_service.AutoBattleError) as exc:
        await callback.answer(str(exc), show_alert=True)
        return
    except economy_service.InsufficientFunds as exc:
        await callback.answer(str(exc), show_alert=True)
        return

    if result["status"] != "resolved":
        await callback.answer(f"Автобитва #{autobattle_id} уже {result['status']}.", show_alert=True)
        return

    await callback.message.edit_text(_result_text(autobattle_id, result), parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data.startswith("autobattle_decline:"))
async def autobattle_decline_callback(callback: CallbackQuery, session: AsyncSession) -> None:
    """Кнопка «❌ Отклонить» — тот же `decline_autobattle`, что
    `/autobattle_decline`."""
    if callback.from_user is None or callback.message is None or callback.data is None:
        return
    try:
        autobattle_id = int(callback.data.split(":", 1)[1])
    except ValueError:
        await callback.answer("Некорректная автобитва.", show_alert=True)
        return

    try:
        result = await autobattle_service.decline_autobattle(
            session, callback.message.chat.id, autobattle_id, callback.from_user.id
        )
    except (autobattle_service.AutoBattleNotFound, autobattle_service.AutoBattleError) as exc:
        await callback.answer(str(exc), show_alert=True)
        return

    if result["status"] != "declined":
        await callback.answer(f"Автобитва #{autobattle_id} уже {result['status']}.", show_alert=True)
        return

    await callback.message.edit_text(f"Автобитва #{autobattle_id} отклонена, ставка возвращена.")
    await callback.answer()
