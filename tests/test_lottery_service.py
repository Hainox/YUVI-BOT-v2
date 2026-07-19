"""Интеграционные тесты ежедневной лотереи `/yuvi` (LOTTERY-01) против живого
Postgres (фикстура `session`, форма tests/test_victim_service.py). Доказывают:

- Идемпотентность пика по MSK-дню (D-09): повторный run_lottery в тот же день
  возвращает ТОГО ЖЕ Yuvi_Yuvi дня (is_new=False), не перевыбирает.
- Кандидаты — ТОЛЬКО вчерашние активные участники (distinct daily_stats.user_id
  за stat_date=вчера), не все подряд — активный только позавчера не кандидат.
- Новый MSK-день = новый пик (self-reset по UNIQUE(chat_id, kind, day_msk),
  Pitfall 4) — не требует явного DELETE-сброса.
- Announcement-only (D-10): run_lottery не двигает деньги (нет строк
  economy_tx для chat_id).
- expires_at строки = конец текущего MSK-дня (23:59:59) — safety-net на
  случай пропущенного тика планировщика (Pitfall 4, Success Criterion 3).
"""

from __future__ import annotations

from datetime import date
from datetime import datetime
from datetime import time
from datetime import timedelta

import pytest
from sqlalchemy import func
from sqlalchemy import select

from bot.services import daily_pick_service
from bot.services import lottery_service
from common.models.daily_pick import DailyPick
from common.models.daily_stat import DailyStat
from common.models.economy_tx import EconomyTx
from common.models.user import User


# --- Хелперы (форма test_victim_service.py) ----------------------------------


async def _ensure_user(session, user_id: int, first_name: str = "Тест") -> None:
    session.add(User(id=user_id, first_name=first_name))
    await session.flush()


async def _seed_daily_stat(session, chat_id: int, user_id: int, stat_date: date) -> None:
    session.add(DailyStat(chat_id=chat_id, user_id=user_id, stat_date=stat_date, message_count=1))
    await session.flush()


async def _economy_tx_count(session, chat_id: int) -> int:
    result = await session.execute(
        select(func.count()).select_from(EconomyTx).where(EconomyTx.chat_id == chat_id)
    )
    return result.scalar_one()


class _ForcedChoiceRng:
    """Тестовый RNG-стаб, monkeypatched вместо `daily_pick_service._rng`.
    Форсирует детерминированный результат `.choice(seq)`."""

    def __init__(self, choice_value):
        self._choice_value = choice_value

    def choice(self, seq):
        return self._choice_value


# --- Идемпотентность пика по MSK-дню (D-09) -----------------------------------


@pytest.mark.asyncio
async def test_lottery_idempotent_same_day(session, monkeypatch):
    chat_id = -1009005001
    uid = 9005001
    await _ensure_user(session, uid, "Yuvi_Yuvi")
    yesterday = daily_pick_service._today_msk() - timedelta(days=1)
    await _seed_daily_stat(session, chat_id, uid, yesterday)
    await session.commit()

    monkeypatch.setattr(daily_pick_service, "_rng", _ForcedChoiceRng(uid))

    first = await lottery_service.run_lottery(session, chat_id)
    second = await lottery_service.run_lottery(session, chat_id)

    assert first["winner"] == uid
    assert first["is_new"] is True
    assert second["winner"] == uid
    assert second["is_new"] is False


# --- Кандидаты — только вчерашние активные ------------------------------------


@pytest.mark.asyncio
async def test_picks_from_yesterday_active(session):
    chat_id = -1009005002
    uid_yesterday_1, uid_yesterday_2 = 9005002, 9005003
    uid_day_before = 9005004
    await _ensure_user(session, uid_yesterday_1, "Вчера1")
    await _ensure_user(session, uid_yesterday_2, "Вчера2")
    await _ensure_user(session, uid_day_before, "Позавчера")

    today = daily_pick_service._today_msk()
    yesterday = today - timedelta(days=1)
    day_before_yesterday = today - timedelta(days=2)

    await _seed_daily_stat(session, chat_id, uid_yesterday_1, yesterday)
    await _seed_daily_stat(session, chat_id, uid_yesterday_2, yesterday)
    await _seed_daily_stat(session, chat_id, uid_day_before, day_before_yesterday)
    await session.commit()

    result = await lottery_service.run_lottery(session, chat_id)

    assert result["winner"] in {uid_yesterday_1, uid_yesterday_2}
    assert result["winner"] != uid_day_before


# --- Новый MSK-день = новый пик (Pitfall 4) -----------------------------------


@pytest.mark.asyncio
async def test_new_day_new_lottery(session, monkeypatch):
    chat_id = -1009005005
    uid1, uid2 = 9005005, 9005006
    await _ensure_user(session, uid1, "День1")
    await _ensure_user(session, uid2, "День2")

    day_n = daily_pick_service._today_msk()
    monkeypatch.setattr(daily_pick_service, "_today_msk", lambda: day_n)
    await _seed_daily_stat(session, chat_id, uid1, day_n - timedelta(days=1))
    await session.commit()

    monkeypatch.setattr(daily_pick_service, "_rng", _ForcedChoiceRng(uid1))
    first = await lottery_service.run_lottery(session, chat_id)
    assert first["winner"] == uid1
    assert first["is_new"] is True
    assert first["day_msk"] == day_n

    day_n_plus_1 = day_n + timedelta(days=1)
    monkeypatch.setattr(daily_pick_service, "_today_msk", lambda: day_n_plus_1)
    # "Вчера" относительно нового дня — это day_n, где активен uid2.
    await _seed_daily_stat(session, chat_id, uid2, day_n)
    await session.commit()
    monkeypatch.setattr(daily_pick_service, "_rng", _ForcedChoiceRng(uid2))

    second = await lottery_service.run_lottery(session, chat_id)
    assert second["winner"] == uid2
    assert second["is_new"] is True
    assert second["day_msk"] == day_n_plus_1


# --- Announcement-only: нет движения денег (D-10) -----------------------------


@pytest.mark.asyncio
async def test_lottery_no_money_movement(session, monkeypatch):
    chat_id = -1009005007
    uid = 9005007
    await _ensure_user(session, uid, "Аноунс")
    yesterday = daily_pick_service._today_msk() - timedelta(days=1)
    await _seed_daily_stat(session, chat_id, uid, yesterday)
    await session.commit()

    monkeypatch.setattr(daily_pick_service, "_rng", _ForcedChoiceRng(uid))

    assert await _economy_tx_count(session, chat_id) == 0
    result = await lottery_service.run_lottery(session, chat_id)
    assert result["winner"] == uid
    assert await _economy_tx_count(session, chat_id) == 0


# --- expires_at = конец текущего MSK-дня (safety-net, Pitfall 4) -------------


@pytest.mark.asyncio
async def test_lottery_expires_at_end_of_day(session, monkeypatch):
    chat_id = -1009005008
    uid = 9005008
    await _ensure_user(session, uid, "Дедлайн")
    yesterday = daily_pick_service._today_msk() - timedelta(days=1)
    await _seed_daily_stat(session, chat_id, uid, yesterday)
    await session.commit()

    monkeypatch.setattr(daily_pick_service, "_rng", _ForcedChoiceRng(uid))

    result = await lottery_service.run_lottery(session, chat_id)

    stored_expires_at = (
        await session.execute(
            select(DailyPick.expires_at).where(
                DailyPick.chat_id == chat_id,
                DailyPick.kind == "lottery",
                DailyPick.day_msk == result["day_msk"],
            )
        )
    ).scalar_one()

    assert stored_expires_at == datetime.combine(result["day_msk"], time(23, 59, 59))
