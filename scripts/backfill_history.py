"""Standalone Kurigram entrypoint для backfill исторических сообщений (DATA-04).

Что делает: вызывает backfill_service.run_backfill напрямую — никакой
дублирующей Kurigram-логики, только обёртка в asyncio.run.

Когда использовать: ручной прогон backfill вне /backfill-команды (например,
до первого запуска бота в чате, или для повторной сверки истории).

Что нужно настроить перед запуском:
- TG_API_ID и TG_API_HASH в .env — креды ЛИЧНОГО аккаунта пользователя с
  https://my.telegram.org -> API development tools (НЕ токен бота).
- Первый запуск создаст файл MTProto-сессии — потребуется ввести код
  подтверждения из Telegram при интерактивном запуске.

Запуск:
    python scripts/backfill_history.py <chat_id>
    # или без аргумента — возьмёт CHAT_ID из .env
"""

from __future__ import annotations

import asyncio
import sys

from bot.config import settings
from bot.services.backfill_service import run_backfill


def _resolve_chat_id() -> int:
    if len(sys.argv) > 1:
        return int(sys.argv[1])
    return settings.chat_id


if __name__ == "__main__":
    asyncio.run(run_backfill(_resolve_chat_id()))
