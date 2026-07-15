"""FastAPI-зависимости аутентификации Mini App: initData HMAC + membership/admin (D-01).

`validate_init_data` реализует криптопроверку подписи initData по алгоритму
Telegram (docs.telegram-mini-apps.com/platform/init-data,
core.telegram.org/bots/webapps): `secret_key = HMAC-SHA256("WebAppData",
bot_token)`, затем `HMAC-SHA256(secret_key, data_check_string)` сравнивается
с присланным `hash` через `hmac.compare_digest` — constant-time сравнение
(T-04-06), НЕ оператором `==` (timing-атака).

`require_membership`/`require_admin` вызывают `api/telegram_client.py::
get_chat_member_status` — ту же живую проверку, что и `bot/services/
admin_service.py::is_chat_admin` (см. докстринг telegram_client.py про
задокументированное расхождение по TTL-кэшу, D-01). `user_id` ВСЕГДА
берётся из провалидированного initData, никогда из query-параметров —
это закрывает IDOR (T-04-08): клиент не может подменить, чей баланс/
статус членства он проверяет, подставив чужой user_id в query.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from urllib.parse import parse_qsl

from fastapi import HTTPException
from fastapi import Request

from api import telegram_client
from bot.config import settings


class InvalidInitData(Exception):
    """initData не прошёл криптопроверку или истёк по TTL.

    Маппится в 401 через `api/main.py::handle_invalid_init_data`; текст
    исключения наружу клиенту не отдаётся (Information Disclosure).
    """


@dataclass(frozen=True)
class AuthContext:
    """Результат успешной проверки членства/админства (D-01)."""

    user_id: int
    chat_id: int
    status: str


def validate_init_data(init_data: str, bot_token: str, ttl_seconds: int) -> dict:
    """Проверяет HMAC-подпись initData и её срок годности (T-04-04/T-04-05).

    Возвращает распарсенные поля (без `hash`) при успехе; иначе поднимает
    `InvalidInitData` с коротким машинным описанием причины.
    """
    try:
        parsed = dict(parse_qsl(init_data, strict_parsing=True))
    except ValueError as exc:
        raise InvalidInitData("malformed init data") from exc
    received_hash = parsed.pop("hash", None)
    if received_hash is None:
        raise InvalidInitData("no hash")

    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(parsed.items()))
    secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    computed_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()

    if not hmac.compare_digest(computed_hash, received_hash):
        raise InvalidInitData("hash mismatch")

    auth_date = int(parsed.get("auth_date", 0))
    if time.time() - auth_date > ttl_seconds:
        raise InvalidInitData("expired")

    return parsed


def extract_init_data(request: Request) -> str:
    """initData из заголовка `X-Telegram-Init-Data` ИЛИ query-параметра
    `init_data` (для SSE-роута — `EventSource` не умеет кастомные заголовки)."""
    init_data = request.headers.get("X-Telegram-Init-Data")
    if init_data:
        return init_data
    init_data = request.query_params.get("init_data")
    if init_data:
        return init_data
    raise InvalidInitData("missing init data")


def _resolve_user_id(parsed: dict) -> int:
    try:
        user = json.loads(parsed["user"])
        return int(user["id"])
    except (KeyError, ValueError, TypeError) as exc:
        raise InvalidInitData("missing or malformed user field") from exc


async def require_membership(request: Request) -> AuthContext:
    """Проверяет, что пользователь из initData реально состоит в chat_id
    (query-параметр). IDOR (T-04-08): user_id — только из initData."""
    init_data = extract_init_data(request)
    parsed = validate_init_data(init_data, settings.bot_token, settings.mini_app_init_data_ttl_sec)
    user_id = _resolve_user_id(parsed)
    raw_chat_id = request.query_params.get("chat_id")
    if raw_chat_id is None:
        raise HTTPException(status_code=400, detail="chat_id is required")
    try:
        chat_id = int(raw_chat_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="chat_id must be an integer")

    status = await telegram_client.get_chat_member_status(
        request.app.state.http_client, settings.bot_token, chat_id, user_id
    )
    if status in ("left", "kicked"):
        raise HTTPException(status_code=403, detail="not a chat member")
    return AuthContext(user_id=user_id, chat_id=chat_id, status=status)


async def require_admin(request: Request) -> AuthContext:
    """Как `require_membership`, но требует `is_admin_status` (та же живая
    модель прав, что и бот-сторона, D-01 — не статичный allowlist)."""
    auth = await require_membership(request)
    if not telegram_client.is_admin_status(auth.status):
        raise HTTPException(status_code=403, detail="not a chat admin")
    return auth
