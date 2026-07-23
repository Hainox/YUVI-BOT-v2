"""Регистрация списка команд бота в Telegram (меню при вводе "/").

Два scope: BotCommandScopeDefault — публичный список для всех участников;
BotCommandScopeAllChatAdministrators — тот же список + админские команды
(model_*, prompt_*, backfill, market_resolve/cancel). Telegram не объединяет
списки разных scope между собой, поэтому админский список явно включает и
публичные команды. Сама проверка прав здесь не дублируется — это только
пункты меню, доступ по-прежнему гейтится ChatAdminFilter/admin_service в
хендлерах.
"""

from __future__ import annotations

from aiogram import Bot
from aiogram.types import BotCommand
from aiogram.types import BotCommandScopeAllChatAdministrators
from aiogram.types import BotCommandScopeDefault

_PUBLIC_COMMANDS: list[BotCommand] = [
    BotCommand(command="start", description="Начать работу с ботом"),
    BotCommand(command="casino", description="Открыть казино (Mini App)"),
    BotCommand(command="balance", description="Мой баланс ювиков"),
    BotCommand(command="transfer", description="Перевести ювики участнику"),
    BotCommand(command="leaderboard", description="Топ по балансу"),
    BotCommand(command="economy", description="Сводка экономики чата"),
    BotCommand(command="rules", description="Правила начисления ювиков"),
    BotCommand(command="markets", description="Активные рынки предсказаний"),
    BotCommand(command="market", description="Детали рынка"),
    BotCommand(command="market_create", description="Создать рынок предсказаний"),
    BotCommand(command="market_import", description="Импортировать рынок"),
    BotCommand(command="bet", description="Сделать ставку на рынке"),
    BotCommand(command="portfolio", description="Мой портфель ставок"),
    BotCommand(command="mystats", description="Моя статистика в чате"),
    BotCommand(command="chatstats", description="Статистика чата"),
    BotCommand(command="who", description="Топ участников чата"),
    BotCommand(command="streak", description="Моя серия активности"),
    BotCommand(command="peakday", description="Пиковый день активности"),
    BotCommand(command="words", description="Топ слов в чате"),
    BotCommand(command="mood", description="Настроение чата"),
    BotCommand(command="toxic", description="Топ по токсичности"),
    BotCommand(command="ask", description="Спросить AI"),
    BotCommand(command="card", description="Профиль-карточка участника"),
    BotCommand(command="digest", description="Дайджест чата"),
    BotCommand(command="summary", description="Саммари обсуждения"),
    BotCommand(command="topics", description="Топ тем в чате"),
    BotCommand(command="phrase", description="Случайная цитата"),
    BotCommand(command="joke", description="Шутка"),
    BotCommand(command="duel", description="Вызвать участника на дуэль"),
    BotCommand(command="duelbot", description="Дуэль против банка чата"),
    BotCommand(command="duel_accept", description="Принять дуэль"),
    BotCommand(command="duel_decline", description="Отклонить дуэль"),
    BotCommand(command="duel_cancel", description="Отменить свою дуэль"),
    BotCommand(command="exchange", description="Биржа: список открытых листингов"),
    BotCommand(command="exchange_create", description="Выставить ювики на биржу"),
    BotCommand(command="exchange_claim", description="Заклеймить листинг биржи"),
    BotCommand(command="exchange_cancel", description="Отменить свой листинг биржи"),
    BotCommand(command="exchange_confirm", description="Подтвердить сделку на бирже"),
    BotCommand(command="donate", description="Задонатить звёздами Telegram"),
    BotCommand(command="fb", description="Оставить фидбек, баг или идею"),
    BotCommand(command="poke", description="Толкнуть участника"),
    BotCommand(command="hug", description="Обнять участника"),
    BotCommand(command="joke_order", description="Заказать шутку про участника"),
    BotCommand(command="roast", description="Роастнуть участника"),
    BotCommand(command="tag_rent", description="Арендовать титул в чате"),
    BotCommand(command="tag_cancel", description="Отменить аренду титула"),
    BotCommand(command="twin", description="Спросить AI-двойника участника"),
    BotCommand(command="twin_optin", description="Подключить своего AI-двойника"),
    BotCommand(command="twin_status", description="Статус моего AI-двойника"),
    BotCommand(command="twin_pause", description="Приостановить AI-двойника"),
    BotCommand(command="twin_resume", description="Возобновить AI-двойника"),
    BotCommand(command="twin_optout", description="Отключить AI-двойника"),
    BotCommand(command="victim", description="Пидор дня"),
    BotCommand(command="awards", description="Итоги дня (номинации)"),
    BotCommand(command="yuvi", description="Yuvi_Yuvi дня (лотерея)"),
]

_ADMIN_ONLY_COMMANDS: list[BotCommand] = [
    BotCommand(command="unmute", description="[Админ] Досрочно снять мут после дуэли"),
    BotCommand(command="farmwipe", description="[Админ] Сбросить ферму участника"),
    BotCommand(command="market_resolve", description="[Админ] Разрешить рынок"),
    BotCommand(command="market_cancel", description="[Админ] Отменить рынок"),
    BotCommand(command="exchange_admin_cancel", description="[Админ] Спор биржи: отменить листинг"),
    BotCommand(command="exchange_admin_release", description="[Админ] Спор биржи: завершить сделку"),
    BotCommand(command="backfill", description="[Админ] Загрузить историю чата"),
    BotCommand(command="model_show", description="[Админ] Текущая AI-модель"),
    BotCommand(command="model_list", description="[Админ] Список AI-моделей"),
    BotCommand(command="model_set", description="[Админ] Сменить AI-модель"),
    BotCommand(command="prompt_show", description="[Админ] Текущий промпт"),
    BotCommand(command="prompt_set", description="[Админ] Задать промпт"),
    BotCommand(command="prompt_reset", description="[Админ] Сбросить промпт"),
]


async def setup_bot_commands(bot: Bot) -> None:
    """Синхронизирует меню команд с Telegram.

    Обычные участники видят публичный список, администраторы чата — публичный
    список плюс админские команды.
    """
    await bot.set_my_commands(_PUBLIC_COMMANDS, scope=BotCommandScopeDefault())
    await bot.set_my_commands(
        _PUBLIC_COMMANDS + _ADMIN_ONLY_COMMANDS,
        scope=BotCommandScopeAllChatAdministrators(),
    )
