"""Raw-httpx `restrictChatMember` mute application from the `api` process
(Pitfall 7, T-04.2-11) — the `api` process has no aiogram `Bot` instance
(and must not create one just for a single REST call, same discipline as
`api/telegram_client.py`), so the Mini App accept-duel path needs a
parallel httpx-only implementation of the mute side-effect that
`bot/handlers/duel.py::_apply_mute` already applies for the bot-command
flow.

Fail-closed by design: any non-200 Telegram response or network/exception
is logged and swallowed, NEVER raised — `duel_service.accept_duel`/
`duelbot` already committed the money movement before this is called, so a
Telegram-side mute failure (e.g. loser is a chat admin, which Telegram
rejects for `restrictChatMember`) must never turn an already-successful
accept/duelbot response into an error for the client.
"""

from __future__ import annotations

import logging
from datetime import datetime

import httpx

logger = logging.getLogger(__name__)


async def apply_mute_from_api(
    client: httpx.AsyncClient,
    bot_token: str,
    chat_id: int,
    user_id: int,
    until_date: datetime,
    permissions: dict,
) -> None:
    """POSTs `restrictChatMember` directly to the Telegram Bot API.

    Mirrors `api/telegram_client.py::get_chat_member_status`'s httpx shape
    (same base URL pattern, same fail-closed discipline) but for the mute
    side-effect instead of a membership read. `permissions` is the shared
    `bot.services.duel_constants.MUTE_PERMISSIONS` plain dict — sent as-is
    as Telegram's `permissions` field (no `ChatPermissions` object needed,
    this module deliberately has no aiogram dependency).
    """
    try:
        resp = await client.post(
            f"https://api.telegram.org/bot{bot_token}/restrictChatMember",
            json={
                "chat_id": chat_id,
                "user_id": user_id,
                "permissions": permissions,
                "until_date": int(until_date.timestamp()),
            },
        )
    except Exception:
        logger.exception(
            "apply_mute_from_api: restrictChatMember request failed chat_id=%s user_id=%s",
            chat_id,
            user_id,
        )
        return

    if resp.status_code != 200:
        logger.warning(
            "apply_mute_from_api: restrictChatMember non-200 chat_id=%s user_id=%s status=%s body=%s",
            chat_id,
            user_id,
            resp.status_code,
            resp.text,
        )
