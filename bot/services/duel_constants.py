"""Shared duel mute/unmute constants (D-01/D-02) — reused by BOTH
`bot/handlers/duel.py` (aiogram `Bot.restrict_chat_member`, builds
`ChatPermissions(**MUTE_PERMISSIONS)`) and `api/duel_mute.py` (raw-httpx
`restrictChatMember`, sends `MUTE_PERMISSIONS` as-is inside the JSON body).

Plain `str`/`dict` constants ONLY — deliberately NO `aiogram` import here.
The `api` process must be able to import this module without pulling in
`aiogram` (04.2-RESEARCH.md Anti-Pattern: "Импорт `bot/handlers/duel.py::
_apply_mute` напрямую в `api`: невозможно без aiogram в
`api/requirements.txt`"), so this module stays the single shared source of
truth for the permission literals without dragging the bot-side dependency
into the FastAPI process.
"""

from __future__ import annotations

# Claude's Discretion (04.1-CONTEXT.md, carried over verbatim from
# bot/handlers/duel.py): placeholder file_ids — a domain detail, not a
# money/mute-logic concern. Replace with real project stickers at any time.
MUTE_STICKER_ID = "CAACAgIAAxkBAAEL_duel_mute_placeholder_sticker_id"
UNMUTE_STICKER_ID = "CAACAgIAAxkBAAEL_duel_unmute_placeholder_sticker_id"

# Field names match Telegram Bot API's ChatPermissions object exactly — the
# bot side builds `ChatPermissions(**MUTE_PERMISSIONS)`, the api side sends
# this dict verbatim as the `permissions` field of a raw restrictChatMember
# POST body.
MUTE_PERMISSIONS: dict[str, bool] = {
    "can_send_messages": False,
    "can_send_audios": False,
    "can_send_documents": False,
    "can_send_photos": False,
    "can_send_videos": False,
    "can_send_video_notes": False,
    "can_send_voice_notes": False,
    "can_send_polls": False,
    "can_send_other_messages": False,
    "can_add_web_page_previews": False,
}

UNMUTE_PERMISSIONS: dict[str, bool] = {
    "can_send_messages": True,
    "can_send_audios": True,
    "can_send_documents": True,
    "can_send_photos": True,
    "can_send_videos": True,
    "can_send_video_notes": True,
    "can_send_voice_notes": True,
    "can_send_polls": True,
    "can_send_other_messages": True,
    "can_add_web_page_previews": True,
}
