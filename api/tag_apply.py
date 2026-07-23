"""Raw-httpx `setChatMemberTag` adapter for the `api` process (TAG-02 Mini
App rental routes, mirrors `api/duel_mute.py`'s reasoning) — the `api`
process has no aiogram `Bot` instance and must not create one just for a
single REST call (same discipline as `api/telegram_client.py`).

`bot/services/tag_service.py::grant_title`/`clear_title` (and
`tag_rental_service.rent_title`/`cancel_rental`, which delegate to them)
take a `TagBotLike` Protocol — a duck-typed stand-in for the one Bot API
call this side of the codebase needs (`set_chat_member_tag`). A real
aiogram `Bot` already satisfies it structurally; `ApiTagBot` below is the
`api`-process implementation of the same Protocol, backed by raw httpx
instead.

Fail-closed by design, matching `tag_service._set_tag`/`_demote`'s own
discipline: those wrap the real aiogram call in
`except (TelegramBadRequest, TelegramForbiddenError)`, which never fires
for httpx (different exception types) — so this adapter has to swallow its
own failures internally and return `False` instead of raising. Money/state
(`active_titles`) is already committed by the time this runs; a Telegram-side
failure (bot lacks `can_manage_tags`, network hiccup) must never turn an
already-successful rent/cancel response into an error for the client.
"""

from __future__ import annotations

import logging

import httpx

logger = logging.getLogger(__name__)


class ApiTagBot:
    """`TagBotLike` implementation for the `api` process — POSTs
    `setChatMemberTag` directly to the Telegram Bot API via the shared
    singleton `httpx.AsyncClient` (`request.app.state.http_client`, never a
    fresh client per request, same rule as `api/telegram_client.py`)."""

    def __init__(self, client: httpx.AsyncClient, bot_token: str) -> None:
        self._client = client
        self._bot_token = bot_token

    async def set_chat_member_tag(self, chat_id: int, user_id: int, tag: str | None = None) -> bool:
        try:
            resp = await self._client.post(
                f"https://api.telegram.org/bot{self._bot_token}/setChatMemberTag",
                json={"chat_id": chat_id, "user_id": user_id, "tag": tag},
            )
        except Exception:
            logger.exception(
                "ApiTagBot.set_chat_member_tag: request failed chat_id=%s user_id=%s", chat_id, user_id
            )
            return False

        if resp.status_code != 200:
            logger.warning(
                "ApiTagBot.set_chat_member_tag: non-200 chat_id=%s user_id=%s status=%s body=%s",
                chat_id,
                user_id,
                resp.status_code,
                resp.text,
            )
            return False

        return True
