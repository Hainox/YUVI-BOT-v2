from __future__ import annotations

from datetime import date
from types import SimpleNamespace

from bot.services.arena_daily_awards_service import daily_bounds
from bot.services.arena_digest_service import _name
from bot.services.arena_digest_service import _winner_line
from common.models.user import User


def test_arena_digest_day_uses_moscow_midnight_window() -> None:
    start, end = daily_bounds(date(2026, 8, 8))

    assert start.isoformat() == "2026-08-08T00:00:00+03:00"
    assert end.isoformat() == "2026-08-09T00:00:00+03:00"


def test_arena_digest_uses_username_or_first_name_without_bets() -> None:
    user = User(id=42, username="fighter", first_name="Имя")
    match = SimpleNamespace(match_result="win", winner_id=42)

    assert _name(user, user.id) == "@fighter"
    assert _winner_line(match, {42: user}) == "@fighter"
    assert "bet" not in _winner_line(match, {42: user}).lower()
