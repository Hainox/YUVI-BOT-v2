"""Admin-команды смены AI-модели/промпта на лету (AI-08, D-08).

Гейт — ChatAdminFilter, навешенный на весь роутер: тот же live-механизм
проверки (bot.get_chat_member через admin_service.is_chat_admin), что и у
/backfill в Фазе 1. Никакого нового механизма авторизации/allowlist не
заводится (D-08). Хендлеры тонкие: парс аргумента -> settings_service ->
ответ; вся KV-логика (кэш, upsert) живёт в settings_service.
"""

from __future__ import annotations

import html

from aiogram import Router
from aiogram.filters import Command
from aiogram.filters import CommandObject
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import settings
from bot.filters.chat_admin import ChatAdminFilter
from bot.services import ai_client
from bot.services import settings_service

router = Router()
router.message.filter(ChatAdminFilter())


def _available_models() -> list[str]:
    """Тонкая обёртка над ai_client.available_models() (единственное место
    парсинга settings.ai_available_models — раньше дублировалось здесь)."""
    return ai_client.available_models()


@router.message(Command("model_show"))
async def model_show_command(message: Message, session: AsyncSession) -> None:
    """Показывает ОБЕ модели раздельно (найдено 2026-07-28: kimi-k2.6 падает
    AIEmptyResponseError на строгих промптах topics/phrase/joke/итд, поэтому
    settings_service.get_active_model теперь резолвит разный дефолт в
    зависимости от вызывающего сервиса — twin_service vs все остальные, см.
    bot/config.py). /model_set по-прежнему один общий override на чат —
    перекрывает ОБЕ строки сразу, если админ явно его ставил."""
    twin_model = await settings_service.get_active_model(session, message.chat.id)
    structured_model = await settings_service.get_active_model(
        session, message.chat.id, default=settings.ai_structured_model
    )
    await message.answer(
        f"Модель двойника (/twin): <b>{html.escape(twin_model)}</b>\n"
        f"Модель остальных AI-функций: <b>{html.escape(structured_model)}</b>",
        parse_mode="HTML",
    )


@router.message(Command("model_list"))
async def model_list_command(message: Message) -> None:
    models = _available_models()
    lines = ["<b>Доступные модели</b>"]
    lines.extend(f"- {html.escape(m)}" for m in models)
    await message.answer("\n".join(lines), parse_mode="HTML")


@router.message(Command("model_health"))
async def model_health_command(message: Message) -> None:
    """Число отказов по моделям (AIEmptyResponseError/таймаут/сеть/rate-limit/
    5xx/не найдена), из-за которых complete_with_fallback переключился на
    следующую модель каталога, с момента последнего запуска процесса бота
    (in-memory счётчик, не персистентный — см. ai_client.get_failure_counts,
    тех.долг "надёжность AI-фич", запрошено 2026-07-29)."""
    counts = ai_client.get_failure_counts()
    if not counts:
        await message.answer("Отказов моделей с последнего запуска бота не было.")
        return

    lines = ["<b>Отказы моделей с последнего запуска бота</b>"]
    for model, count in sorted(counts.items(), key=lambda kv: kv[1], reverse=True):
        lines.append(f"- {html.escape(model)}: {count}")
    await message.answer("\n".join(lines), parse_mode="HTML")


@router.message(Command("model_set"))
async def model_set_command(message: Message, command: CommandObject, session: AsyncSession) -> None:
    if message.from_user is None:
        return

    model_id = (command.args or "").strip()
    if not model_id:
        await message.reply("Использование: /model_set <id модели>, см. /model_list")
        return

    if model_id not in _available_models():
        await message.reply("Неизвестная модель, см. /model_list")
        return

    await settings_service.set_setting(
        session, message.chat.id, settings_service.KEY_MODEL, model_id, message.from_user.id
    )
    await message.answer(f"AI-модель обновлена: <b>{html.escape(model_id)}</b>", parse_mode="HTML")


@router.message(Command("prompt_show"))
async def prompt_show_command(message: Message, session: AsyncSession) -> None:
    prompt = await settings_service.get_active_prompt(session, message.chat.id)
    await message.answer(
        f"Текущий системный промпт:\n<code>{html.escape(prompt)}</code>", parse_mode="HTML"
    )


@router.message(Command("prompt_set"))
async def prompt_set_command(message: Message, command: CommandObject, session: AsyncSession) -> None:
    if message.from_user is None:
        return

    text = (command.args or "").strip()
    if not text:
        await message.reply("Использование: /prompt_set <текст промпта>")
        return

    await settings_service.set_setting(
        session, message.chat.id, settings_service.KEY_PROMPT, text, message.from_user.id
    )
    await message.answer("Системный промпт обновлён.")


@router.message(Command("prompt_reset"))
async def prompt_reset_command(message: Message, session: AsyncSession) -> None:
    if message.from_user is None:
        return

    await settings_service.set_setting(
        session,
        message.chat.id,
        settings_service.KEY_PROMPT,
        settings.ai_default_system_prompt,
        message.from_user.id,
    )
    await message.answer("Системный промпт сброшен к значению по умолчанию.")
