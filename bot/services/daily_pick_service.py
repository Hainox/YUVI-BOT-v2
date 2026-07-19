"""Общий идемпотентный дневной пик (жертва дня / лотерея) — VICTIM-01/02,
LOTTERY-01, D-09.

`get_or_set_pick` — единственный примитив выбора «кто сегодня выиграл»:
UNIQUE(chat_id, kind, day_msk) из миграции 0008 гарантирует, что повторный
вызов в тот же MSK-день ВСЕГДА возвращает уже выбранного победителя
(is_new=False), никогда не перевыбирает (05-RESEARCH.md Pitfall 5). Новый
день — структурно свежая строка (day_msk входит в уникальный ключ), явный
DELETE-сброс не нужен (Pitfall 4).

Исход — ТОЛЬКО через модульный RNG-seam `_rng` (secrets.SystemRandom(), форма
duel_service._rng) — server-authoritative, подменяется в тестах
monkeypatch'ем. День — через `_today_msk()` (Europe/Moscow), тоже
monkeypatchable seam для тестов "смена дня"; переиспользуется victim_service
(05-04) и лотереей (05-05).
"""

from __future__ import annotations

import secrets
from datetime import date
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from common.models.daily_pick import DailyPick

MSK = ZoneInfo("Europe/Moscow")

_rng = secrets.SystemRandom()


def _today_msk() -> date:
    return datetime.now(MSK).date()


async def get_or_set_pick(
    session: AsyncSession,
    chat_id: int,
    kind: str,
    candidates: list[int],
    expires_at: datetime | None = None,
) -> tuple[int, bool]:
    """Идемпотентный get-or-set пика по (chat_id, kind, day_msk).

    Возвращает (winner_user_id, is_new). Если строка на сегодня уже есть —
    возвращает существующего победителя (is_new=False), `expires_at`
    аргумент в этом случае игнорируется (он пишется только при создании
    новой строки). Иначе выбирает `_rng.choice(candidates)` и вставляет
    строку через `on_conflict_do_nothing` — при проигранной гонке
    (rowcount==0) перечитывает победителя, которого только что вставил
    конкурентный вызов. Не коммитит — транзакцию завершает вызывающий.
    """
    today = _today_msk()
    existing = (
        await session.execute(
            select(DailyPick.winner_user_id).where(
                DailyPick.chat_id == chat_id,
                DailyPick.kind == kind,
                DailyPick.day_msk == today,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing, False

    winner = _rng.choice(candidates)
    stmt = (
        pg_insert(DailyPick)
        .values(
            chat_id=chat_id,
            kind=kind,
            day_msk=today,
            winner_user_id=winner,
            expires_at=expires_at,
        )
        .on_conflict_do_nothing(index_elements=["chat_id", "kind", "day_msk"])
    )
    result = await session.execute(stmt)
    if result.rowcount == 0:
        # Гонка: конкурентный вызов уже вставил строку — перечитываем
        # реального победителя вместо нашего локально выбранного.
        winner = (
            await session.execute(
                select(DailyPick.winner_user_id).where(
                    DailyPick.chat_id == chat_id,
                    DailyPick.kind == kind,
                    DailyPick.day_msk == today,
                )
            )
        ).scalar_one()
        return winner, False
    return winner, True
