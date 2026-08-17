"""Admin-команды смены AI-модели/промпта на лету (AI-08, D-08)."""

from __future__ import annotations

import html
from math import ceil

from aiogram import Router
from aiogram.filters import Command
from aiogram.filters import CommandObject
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import settings
from bot.filters.chat_admin import ChatAdminFilter
from bot.services import ai_client
from bot.services import embed_worker
from bot.services import settings_service

router = Router()
router.message.filter(ChatAdminFilter())


def _available_models() -> list[str]:
    return ai_client.available_models()


@router.message(Command("model_show"))
async def model_show_command(message: Message, session: AsyncSession) -> None:
    model = await settings_service.get_active_model(
        session, message.chat.id, default=settings.ai_structured_model
    )
    await message.answer(
        f"Модель AI-функций: <b>{html.escape(model)}</b>", parse_mode="HTML"
    )


@router.message(Command("model_list"))
async def model_list_command(message: Message) -> None:
    models = _available_models()
    lines = ["<b>Доступные модели</b>"]
    lines.extend(f"- {html.escape(model)}" for model in models)
    await message.answer("\n".join(lines), parse_mode="HTML")


@router.message(Command("model_health"))
async def model_health_command(message: Message) -> None:
    counts = ai_client.get_failure_counts()
    if not counts:
        await message.answer("Отказов моделей с последнего запуска бота не было.")
        return

    lines = ["<b>Отказы моделей с последнего запуска бота</b>"]
    for model, count in sorted(counts.items(), key=lambda item: item[1], reverse=True):
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


@router.message(Command("explicit_on"))
async def explicit_on_command(message: Message, session: AsyncSession) -> None:
    if message.from_user is None:
        return
    await settings_service.set_explicit_language(session, message.chat.id, True, message.from_user.id)
    await session.commit()
    await message.answer("Explicit-лексика разрешена для AI-ответов этого чата.")


@router.message(Command("explicit_off"))
async def explicit_off_command(message: Message, session: AsyncSession) -> None:
    if message.from_user is None:
        return
    await settings_service.set_explicit_language(session, message.chat.id, False, message.from_user.id)
    await session.commit()
    await message.answer("Explicit-лексика отключена для AI-ответов этого чата.")


@router.message(Command("embed_stats"))
async def embed_stats_command(message: Message, session: AsyncSession) -> None:
    """Прогресс пересчёта BGE-M3 эмбеддингов — готовность поиска /q."""
    total, embedded, pending = await embed_worker.get_embed_stats(session, message.chat.id)

    if total == 0:
        await message.answer(
            "<b>Эмбеддинги:</b> сообщений с текстом пока нет — нечего пересчитывать.",
            parse_mode="HTML",
        )
        return

    percent = embedded / total * 100
    if pending == 0:
        eta = "готово"
        ready = "Да — поиск /q покрывает всю историю чата."
    else:
        seconds = ceil(pending / embed_worker._BATCH_SIZE) * 45
        minutes = ceil(seconds / 60)
        if minutes >= 60:
            eta = f"~{minutes // 60} ч {minutes % 60} мин"
        else:
            eta = f"~{minutes} мин"
        ready = "Нет — ещё досчитываются векторы, часть истории пока вне поиска."

    await message.answer(
        "<b>Пересчёт BGE-M3 эмбеддингов</b>\n"
        f"Всего сообщений: <b>{total}</b>\n"
        f"Готово: <b>{embedded}</b> ({percent:.1f}%)\n"
        f"Осталось: <b>{pending}</b>\n"
        f"До готовности: <b>{eta}</b>\n"
        f"/q готов: {ready}",
        parse_mode="HTML",
    )


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
