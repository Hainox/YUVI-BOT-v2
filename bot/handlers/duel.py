"""Команды дуэлей (DUEL-01/DUEL-02): /duel /duelbot /duel_accept /duel_decline
/duel_cancel /unmute. Тонкий хендлер: парсит вход, зовёт duel_service,
форматирует и отвечает — вся денежная/статусная логика в сервисе. Единственная
логика, которую владеет ИМЕННО этот модуль — Telegram-побочный эффект мута
проигравшего (restrictChatMember + send_sticker), применяемый ПОСЛЕ того, как
duel_service вернул результат резолюции (форма markets.py + external_markets.py:
"хендлер оркеструет побочные эффекты, сервис владеет деньгами/статусом").

Кнопки принятия/отклонения (запрошено пользователем 2026-07-23, "свести
написание команд к минимуму"): `/duel` теперь прикрепляет inline-клавиатуру
(`_duel_keyboard`) прямо к сообщению вызова — оппоненту не нужно набирать
`/duel_accept <id>` руками. `duel_accept_callback`/`duel_decline_callback`
ниже вызывают ТЕ ЖЕ `duel_service.accept_duel`/`decline_duel`, что и текстовые
команды (нулевое дублирование бизнес-логики) — IDOR-защита не в хендлере, а
уже внутри сервиса (`opponent_id != accept_duel(...)`-аргумент), поэтому
поддельный `callback_data` с чужим `duel_id` не даёт принять чужую дуэль,
максимум — предсказуемую ошибку. Текстовые `/duel_accept`/`/duel_decline`
НЕ удалены — работают как есть, кнопки лишь основной путь, не единственный.

Мут проигравшего — D-01 (фиксированные 10 минут, MUTE_SECONDS сервиса) + D-02
(стикер при муте и при снятии — MUTE_STICKER_ID/UNMUTE_STICKER_ID ниже,
плейсхолдер file_id, Claude's Discretion по CONTEXT.md, заменяемо позже без
денежных последствий). Авто-снятие ограничения делает сам Telegram по
until_date; APScheduler здесь используется ТОЛЬКО чтобы прислать стикер снятия
в нужный момент (get_scheduler().add_job(..., "date", run_date=until)).

/unmute — D-03: ручной гейт admin_service.is_chat_admin с явным ответом
не-админу (форма bot/handlers/backfill.py/markets.py, не молчаливый
ChatAdminFilter) — любой ТЕКУЩИЙ админ чата может досрочно снять мут.

Все пользовательские имена (first_name) прогоняются через html.escape перед
вставкой в parse_mode="HTML"-ответ (T-04.1-27, конвенция economy.py/markets.py).
"""

from __future__ import annotations

import html
import logging
from datetime import datetime
from datetime import timedelta

from aiogram import Bot
from aiogram import F
from aiogram import Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery
from aiogram.types import ChatPermissions
from aiogram.types import InlineKeyboardButton
from aiogram.types import InlineKeyboardMarkup
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.services import admin_service
from bot.services import duel_constants
from bot.services import duel_service
from bot.services import economy_service
from bot.services.scheduler import get_scheduler
from bot.services.target_resolution_service import resolve_target

logger = logging.getLogger(__name__)

router = Router()

# 04.2-06: MUTE_STICKER_ID/UNMUTE_STICKER_ID и permission-словари живут
# в bot/services/duel_constants.py (общий модуль, переиспользуется
# api/duel_mute.py для httpx-мута из Mini App accept, T-04.2-11) — здесь
# только строим aiogram ChatPermissions из тех же плейн-dict литералов,
# без дублирования.
MUTE_STICKER_ID = duel_constants.MUTE_STICKER_ID
UNMUTE_STICKER_ID = duel_constants.UNMUTE_STICKER_ID

_MUTE_PERMISSIONS = ChatPermissions(**duel_constants.MUTE_PERMISSIONS)
_UNMUTE_PERMISSIONS = ChatPermissions(**duel_constants.UNMUTE_PERMISSIONS)


# --- Мут/размут (побочный эффект хендлера, D-01/D-02) ------------------------


async def _apply_mute(bot: Bot, chat_id: int, user_id: int, seconds: int) -> None:
    until = datetime.utcnow() + timedelta(seconds=seconds)
    await bot.restrict_chat_member(chat_id, user_id, permissions=_MUTE_PERMISSIONS, until_date=until)
    await bot.send_sticker(chat_id, MUTE_STICKER_ID)
    get_scheduler().add_job(
        bot.send_sticker,
        "date",
        run_date=until,
        args=[chat_id, UNMUTE_STICKER_ID],
        id=f"duel_unmute_sticker:{chat_id}:{user_id}",
        replace_existing=True,
    )


async def _lift_mute(bot: Bot, chat_id: int, user_id: int) -> None:
    await bot.restrict_chat_member(chat_id, user_id, permissions=_UNMUTE_PERMISSIONS)
    await bot.send_sticker(chat_id, UNMUTE_STICKER_ID)


# --- Разбор входа + резолв цели -----------------------------------------
# `resolve_target` живёт в bot/services/target_resolution_service.py (WR-04,
# 04.2-REVIEW: раньше был продублирован byte-for-byte в economy.py/duel.py/
# farm_admin.py, теперь shared).


def _display_name(message: Message) -> str:
    user = message.from_user
    assert user is not None
    return html.escape(user.first_name or str(user.id))


def _parse_duel_args(message: Message) -> tuple[str | None, int | None]:
    """Парсит `/duel <@user|reply> <ставка>` либо (в ответ на сообщение)
    `/duel <ставка>` — форма economy.py::_parse_transfer_args."""
    if message.text is None:
        return None, None
    tokens = message.text.split()[1:]
    if not tokens:
        return None, None
    amount_token = tokens[-1]
    amount = int(amount_token) if amount_token.isdigit() else None
    target_arg = tokens[0] if len(tokens) >= 2 else None
    return target_arg, amount


def _parse_amount_arg(message: Message) -> int | None:
    """Парсит `/duelbot <ставка>` — единственный целочисленный аргумент."""
    if message.text is None:
        return None
    tokens = message.text.split()[1:]
    if len(tokens) != 1 or not tokens[0].isdigit():
        return None
    return int(tokens[0])


def _parse_id_arg(message: Message) -> int | None:
    """Парсит `/duel_accept|/duel_decline|/duel_cancel <id>`."""
    if message.text is None:
        return None
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip().isdigit():
        return None
    return int(parts[1].strip())


def _duel_keyboard(duel_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Принять", callback_data=f"duel_accept:{duel_id}"),
                InlineKeyboardButton(text="❌ Отклонить", callback_data=f"duel_decline:{duel_id}"),
            ]
        ]
    )


def _parse_single_target_arg(message: Message) -> str | None:
    """Парсит `/unmute <@user>` (без reply) — единственный текстовый токен."""
    if message.text is None:
        return None
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        return None
    token = parts[1].strip().split()[0] if parts[1].strip() else ""
    return token or None


# --- Хендлеры -----------------------------------------------------------


@router.message(Command("duel"))
async def duel_command(message: Message, session: AsyncSession) -> None:
    if message.from_user is None:
        return

    target_arg, amount = _parse_duel_args(message)
    if amount is None:
        await message.answer(
            "Использование: /duel <ставка> (ответом на сообщение соперника) "
            "или /duel @username <ставка>."
        )
        return

    target = await resolve_target(message, session, target_arg)
    if target is None:
        await message.answer(
            "Ответьте на сообщение соперника командой /duel <ставка> "
            "или используйте /duel @username <ставка>."
        )
        return

    opponent_id, opponent_name = target
    if opponent_id == message.from_user.id:
        await message.answer("Нельзя вызвать на дуэль самого себя.")
        return

    ref_id = f"duel:{message.chat.id}:{message.message_id}"
    try:
        duel = await duel_service.create_duel(
            session, message.chat.id, message.from_user.id, opponent_id, amount, ref_id
        )
    except duel_service.DuelError as exc:
        await message.answer(str(exc))
        return
    except economy_service.InsufficientFunds as exc:
        await message.answer(str(exc))
        return

    await message.answer(
        f"<b>Дуэль #{duel.id}:</b> {_display_name(message)} вызывает "
        f"{html.escape(opponent_name)} на ставку {amount} ювиков.",
        parse_mode="HTML",
        reply_markup=_duel_keyboard(duel.id),
    )


@router.message(Command("duelbot"))
async def duelbot_command(message: Message, session: AsyncSession, bot: Bot) -> None:
    if message.from_user is None:
        return

    amount = _parse_amount_arg(message)
    if amount is None:
        await message.answer("Использование: /duelbot <ставка>")
        return

    ref_id = f"duelbot:{message.chat.id}:{message.message_id}"
    try:
        result = await duel_service.duelbot(session, message.chat.id, message.from_user.id, amount, ref_id)
    except duel_service.DuelError as exc:
        await message.answer(str(exc))
        return
    except economy_service.InsufficientFunds as exc:
        await message.answer(str(exc))
        return

    if result["loser_id"] is not None:
        try:
            await _apply_mute(bot, message.chat.id, result["loser_id"], result["mute_seconds"])
        except Exception:
            # WR-05 (04.1-REVIEW): деньги уже двинулись (duel_service.duelbot
            # закоммитил) — падение мута (например, проигравший — админ чата,
            # Telegram отвергает restrictChatMember на админов) не должно
            # съедать ответ пользователю о завершённой дуэли.
            logger.exception("duelbot: не удалось замутить loser_id=%s", result["loser_id"])

    if result["winner_id"] is not None:
        text = (
            f"<b>Дуэль с банком #{result['duel_id']}:</b> вы выиграли {result['pot']} ювиков!"
        )
    else:
        text = (
            f"<b>Дуэль с банком #{result['duel_id']}:</b> увы, банк забрал ставку. "
            f"Мут на {result['mute_seconds'] // 60} мин."
        )
    await message.answer(text, parse_mode="HTML")


@router.message(Command("duel_accept"))
async def duel_accept_command(message: Message, session: AsyncSession, bot: Bot) -> None:
    if message.from_user is None:
        return

    duel_id = _parse_id_arg(message)
    if duel_id is None:
        await message.answer("Использование: /duel_accept <id дуэли>")
        return

    ref_id = f"duel_accept:{message.chat.id}:{message.message_id}"
    try:
        result = await duel_service.accept_duel(
            session, message.chat.id, duel_id, message.from_user.id, ref_id
        )
    except (duel_service.DuelNotFound, duel_service.DuelError) as exc:
        await message.answer(str(exc))
        return
    except economy_service.InsufficientFunds as exc:
        await message.answer(str(exc))
        return

    if result["status"] != "resolved":
        await message.answer(f"Дуэль #{duel_id} уже {result['status']}.")
        return

    if result["loser_id"] is not None:
        try:
            await _apply_mute(bot, message.chat.id, result["loser_id"], result["mute_seconds"])
        except Exception:
            # WR-05 (04.1-REVIEW): та же защита, что в duelbot_command выше —
            # деньги уже двинулись (duel_service.accept_duel закоммитил).
            logger.exception("duel_accept: не удалось замутить loser_id=%s", result["loser_id"])

    await message.answer(
        f"<b>Дуэль #{duel_id} завершена:</b> победитель забирает {result['pot']} ювиков "
        f"(комиссия в банк: {result['fee']}). Проигравший замучен на "
        f"{result['mute_seconds'] // 60} мин.",
        parse_mode="HTML",
    )


@router.message(Command("duel_decline"))
async def duel_decline_command(message: Message, session: AsyncSession) -> None:
    if message.from_user is None:
        return

    duel_id = _parse_id_arg(message)
    if duel_id is None:
        await message.answer("Использование: /duel_decline <id дуэли>")
        return

    try:
        result = await duel_service.decline_duel(session, message.chat.id, duel_id, message.from_user.id)
    except (duel_service.DuelNotFound, duel_service.DuelError) as exc:
        await message.answer(str(exc))
        return

    if result["status"] != "declined":
        await message.answer(f"Дуэль #{duel_id} уже {result['status']}.")
        return
    await message.answer(f"Дуэль #{duel_id} отклонена, ставка возвращена.")


@router.message(Command("duel_cancel"))
async def duel_cancel_command(message: Message, session: AsyncSession) -> None:
    if message.from_user is None:
        return

    duel_id = _parse_id_arg(message)
    if duel_id is None:
        await message.answer("Использование: /duel_cancel <id дуэли>")
        return

    try:
        result = await duel_service.cancel_duel(session, message.chat.id, duel_id, message.from_user.id)
    except (duel_service.DuelNotFound, duel_service.DuelError) as exc:
        await message.answer(str(exc))
        return

    if result["status"] != "cancelled":
        await message.answer(f"Дуэль #{duel_id} уже {result['status']}.")
        return
    await message.answer(f"Дуэль #{duel_id} отменена, ставка возвращена.")


@router.callback_query(F.data.startswith("duel_accept:"))
async def duel_accept_callback(callback: CallbackQuery, session: AsyncSession, bot: Bot) -> None:
    """Кнопка «✅ Принять» на сообщении вызова — тот же `accept_duel`, что
    `/duel_accept`, IDOR-гард уже внутри сервиса (см. модульный docstring)."""
    if callback.from_user is None or callback.message is None or callback.data is None:
        return
    try:
        duel_id = int(callback.data.split(":", 1)[1])
    except ValueError:
        await callback.answer("Некорректная дуэль.", show_alert=True)
        return

    ref_id = f"duel_accept:{callback.message.chat.id}:{callback.id}"
    try:
        result = await duel_service.accept_duel(
            session, callback.message.chat.id, duel_id, callback.from_user.id, ref_id
        )
    except (duel_service.DuelNotFound, duel_service.DuelError) as exc:
        await callback.answer(str(exc), show_alert=True)
        return
    except economy_service.InsufficientFunds as exc:
        await callback.answer(str(exc), show_alert=True)
        return

    if result["status"] != "resolved":
        await callback.answer(f"Дуэль #{duel_id} уже {result['status']}.", show_alert=True)
        return

    if result["loser_id"] is not None:
        try:
            await _apply_mute(bot, callback.message.chat.id, result["loser_id"], result["mute_seconds"])
        except Exception:
            # WR-05 (04.1-REVIEW): та же защита, что в duel_accept_command —
            # деньги уже двинулись (accept_duel закоммитил).
            logger.exception("duel_accept_callback: не удалось замутить loser_id=%s", result["loser_id"])

    await callback.message.edit_text(
        f"<b>Дуэль #{duel_id} завершена:</b> победитель забирает {result['pot']} ювиков "
        f"(комиссия в банк: {result['fee']}). Проигравший замучен на "
        f"{result['mute_seconds'] // 60} мин.",
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data.startswith("duel_decline:"))
async def duel_decline_callback(callback: CallbackQuery, session: AsyncSession) -> None:
    """Кнопка «❌ Отклонить» — тот же `decline_duel`, что `/duel_decline`."""
    if callback.from_user is None or callback.message is None or callback.data is None:
        return
    try:
        duel_id = int(callback.data.split(":", 1)[1])
    except ValueError:
        await callback.answer("Некорректная дуэль.", show_alert=True)
        return

    try:
        result = await duel_service.decline_duel(
            session, callback.message.chat.id, duel_id, callback.from_user.id
        )
    except (duel_service.DuelNotFound, duel_service.DuelError) as exc:
        await callback.answer(str(exc), show_alert=True)
        return

    if result["status"] != "declined":
        await callback.answer(f"Дуэль #{duel_id} уже {result['status']}.", show_alert=True)
        return

    await callback.message.edit_text(f"Дуэль #{duel_id} отклонена, ставка возвращена.")
    await callback.answer()


@router.message(Command("unmute"))
async def unmute_command(message: Message, session: AsyncSession, bot: Bot) -> None:
    """D-03: досрочное снятие дуэльного мута — только текущий (live-проверка)
    админ чата, явный отказ не-админу (не молчаливый фильтр)."""
    if message.from_user is None:
        return
    if not await admin_service.is_chat_admin(bot, message.chat.id, message.from_user.id):
        await message.reply("Только администратор чата может снять мут.")
        return

    target_arg = _parse_single_target_arg(message)
    target = await resolve_target(message, session, target_arg)
    if target is None:
        await message.answer(
            "Использование: /unmute (ответом на сообщение замученного) или /unmute @username"
        )
        return

    target_id, target_name = target
    await _lift_mute(bot, message.chat.id, target_id)
    await message.answer(f"Мут снят для {html.escape(target_name)}.", parse_mode="HTML")
