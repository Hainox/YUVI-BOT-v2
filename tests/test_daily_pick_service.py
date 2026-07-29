"""Юнит-тесты примитива `get_or_set_pick` (daily_pick_service) против живого
Postgres (фикстура `session`, форма tests/test_victim_service.py /
tests/test_lottery_service.py). Тестируют ИМЕННО модуль-примитив, не
консьюмеров (victim/lottery/daily_twin/awards/giveaway — чужая зона):

- Проигранная гонка вставки (`rowcount == 0` после `on_conflict_do_nothing`,
  строки 82-95): функция обязана перечитать РЕАЛЬНО персистентного
  победителя, а не вернуть локально выбранный `_rng.choice(candidates)`
  (05-RESEARCH.md Pitfall 5) — единственная ветка примитива, реально
  задействующая UNIQUE(chat_id, kind, day_msk) как арбитра гонки.
- `expires_at` игнорируется при уже существующей строке (докстринг
  get_or_set_pick, строки 50-52) — повторный вызов с ДРУГИМ expires_at не
  переписывает уже сохранённое значение.
"""

from __future__ import annotations

from datetime import datetime
from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from bot.services import daily_pick_service
from common.models.daily_pick import DailyPick
from common.models.user import User


async def _ensure_user(session, user_id: int, first_name: str = "Тест") -> None:
    session.add(User(id=user_id, first_name=first_name))
    await session.flush()


class _ForcedChoiceRng:
    """Тестовый RNG-стаб, monkeypatched вместо `daily_pick_service._rng`
    (форма tests/test_victim_service.py). Форсирует детерминированный
    результат `.choice(seq)`."""

    def __init__(self, choice_value):
        self._choice_value = choice_value

    def choice(self, seq):
        return self._choice_value


# --- Проигранная гонка вставки: rowcount==0 перечитывает персистентного победителя --


@pytest.mark.asyncio
async def test_get_or_set_pick_lost_insert_race_rereads_persisted_winner(session, monkeypatch):
    """Форсирует ветку `if result.rowcount == 0` (daily_pick_service.py,
    строки 83-95): между собственным SELECT existing (находит None) и
    собственным INSERT ... ON CONFLICT DO NOTHING подсовываем 'конкурентную'
    строку на тот же (chat_id, kind, day_msk) с ДРУГИМ winner_user_id — как
    будто параллельный вызов успел вставить её первым. `_rng` форсирован на
    СВОЙ (проигравший) выбор — функция обязана вернуть персистентного
    конкурентного победителя, а НЕ свой локальный RNG-выбор, и is_new=False.
    Без этой ветки регресс (например, потеря фильтра по `kind` в повторном
    SELECT) привёл бы к подмене победителя/двойной выплате в victim_service —
    ни один существующий тест эту ветку физически не выполняет."""
    chat_id = -1009003001
    kind = "victim"
    my_choice_uid = 9003001
    competitor_uid = 9003002
    await _ensure_user(session, my_choice_uid, "МойВыбор")
    await _ensure_user(session, competitor_uid, "Конкурент")
    await session.commit()

    today = daily_pick_service._today_msk()
    real_execute = session.execute
    call_count = {"n": 0}

    async def _execute_with_injected_race(clause, *args, **kwargs):
        call_count["n"] += 1
        result = await real_execute(clause, *args, **kwargs)
        if call_count["n"] == 1:
            # Это собственный первый SELECT existing внутри get_or_set_pick
            # (нашёл None) — ДО того, как get_or_set_pick успеет сделать свой
            # INSERT, вставляем "конкурентную" строку конкурентного вызова.
            competing_stmt = pg_insert(DailyPick).values(
                chat_id=chat_id,
                kind=kind,
                day_msk=today,
                winner_user_id=competitor_uid,
                expires_at=None,
            )
            await real_execute(competing_stmt)
        return result

    monkeypatch.setattr(session, "execute", _execute_with_injected_race)
    monkeypatch.setattr(daily_pick_service, "_rng", _ForcedChoiceRng(my_choice_uid))

    winner, is_new = await daily_pick_service.get_or_set_pick(
        session, chat_id, kind, candidates=[my_choice_uid, competitor_uid]
    )

    assert winner == competitor_uid
    assert winner != my_choice_uid
    assert is_new is False


# --- expires_at игнорируется при уже существующей строке --------------------


@pytest.mark.asyncio
async def test_get_or_set_pick_ignores_expires_at_on_replay(session, monkeypatch):
    """Докстринг get_or_set_pick обещает: `expires_at` игнорируется, если
    строка на сегодня уже существует — пишется только при создании новой
    строки. Вызываем дважды с ЗАВЕДОМО разными expires_at и проверяем, что в
    БД остаётся именно значение первого вызова (не переписывается вторым)."""
    chat_id = -1009003002
    kind = "victim"
    uid = 9003003
    await _ensure_user(session, uid, "Жертва")
    await session.commit()

    monkeypatch.setattr(daily_pick_service, "_rng", _ForcedChoiceRng(uid))

    first_expires_at = datetime(2026, 1, 1, 12, 0, 0)
    winner1, is_new1 = await daily_pick_service.get_or_set_pick(
        session, chat_id, kind, candidates=[uid], expires_at=first_expires_at
    )
    await session.commit()

    second_expires_at = first_expires_at + timedelta(days=30)
    winner2, is_new2 = await daily_pick_service.get_or_set_pick(
        session, chat_id, kind, candidates=[uid], expires_at=second_expires_at
    )

    stored_expires_at = (
        await session.execute(
            select(DailyPick.expires_at).where(
                DailyPick.chat_id == chat_id,
                DailyPick.kind == kind,
                DailyPick.day_msk == daily_pick_service._today_msk(),
            )
        )
    ).scalar_one()

    assert winner1 == uid
    assert is_new1 is True
    assert winner2 == uid
    assert is_new2 is False
    assert stored_expires_at == first_expires_at
    assert stored_expires_at != second_expires_at
