"""Ежедневные квесты (QUEST-01) — 6 квестов, каждый закрывается за уже
существующие данные (`daily_stats`/`economy_tx`), отдельный трекинг
прогресса не заводится.

Pull-модель: прогресс не считается фоновым тиком, а пересчитывается
`_today_progress` заново при КАЖДОМ вызове `/quests` или
`GET /api/v1/quests` — планировщика/скедулера нет (в отличие от
`awards_service.register_daily_autopost`).

`claim_quests` вставляет строку в `quest_completions` через
`on_conflict_do_nothing(index_elements=[...])` — та же идемпотентная
get-or-set форма, что `daily_pick_service.get_or_set_pick`
(UNIQUE(chat_id, user_id, quest_key, day_msk), день входит в ключ, поэтому
новый MSK-день структурно всегда свежая строка, явный сброс не нужен):
`rowcount == 0` значит квест этого дня уже заклеймлен — награда не
выплачивается повторно.

`quest_completions` служит ДВУМ целям одновременно: идемпотентный гейт
выплаты ВЫШЕ и персистентный источник истины для cumulative-счётчика
выполненных квестов (`get_total_completions`), который
`clicker_service.get_effective_max_level` читает, чтобы разблокировать
бонусные уровни фермы — счётчик персистентный, а не производный запрос "на
лету", поэтому разблокированный уровень фермы никогда не откатывается
назад задним числом.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from datetime import datetime
from datetime import time
from zoneinfo import ZoneInfo

from sqlalchemy import func
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from bot.services import economy_service
from common.models.daily_stat import DailyStat
from common.models.economy_tx import EconomyTx
from common.models.quest_completion import QuestCompletion

MSK = ZoneInfo("Europe/Moscow")
UTC = ZoneInfo("UTC")


@dataclass(frozen=True)
class QuestDef:
    key: str
    label: str
    description: str
    reward: int


QUESTS: tuple[QuestDef, ...] = (
    QuestDef("active", "Активность", "Напиши в чате минимум 10 сообщений сегодня.", 15),
    QuestDef("photo", "Фотограф", "Отправь в чат хотя бы одно фото сегодня.", 15),
    QuestDef(
        "social",
        "Общительный",
        "Используй Тыкнуть/Обнять/Анекдот на заказ/Роаст в Магазине хотя бы раз сегодня.",
        20,
    ),
    QuestDef(
        "media",
        "Медиатека",
        "Скачай видео (TikTok/Instagram/YouTube и т.п.) через бота хотя бы раз сегодня.",
        20,
    ),
    QuestDef("duel", "Дуэлянт", "Сыграй хотя бы одну дуэль на ставку сегодня.", 25),
    QuestDef("gacha", "Азартный", "Сделай хотя бы одну крутку в гаче сегодня.", 20),
    QuestDef(
        "casino",
        "Игрок",
        "Сыграй в любую игру казино (кости/монетка/рулетка/слоты/блэкджек) хотя бы раз сегодня.",
        20,
    ),
    QuestDef("transfer", "Меценат", "Переведи ювики любому участнику чата сегодня.", 15),
    QuestDef("farm", "Фермер", "Купи прокачку или обменяй CP на ферме сегодня.", 15),
    QuestDef("tags", "Модник", "Арендуй тег над своим именем сегодня.", 20),
    QuestDef("exchange", "Делец", "Выставь юви на продажу на бирже сегодня.", 20),
    QuestDef("market", "Провидец", "Создай рынок предсказаний сегодня.", 25),
)

_ECONOMY_KIND_QUESTS: dict[str, tuple[str, ...]] = {
    "social": ("social_poke", "social_hug", "social_joke_order", "social_roast"),
    "media": ("mediadl_charge",),
    "duel": ("duel_stake",),
    "gacha": ("gacha_roll",),
    "casino": ("casino_bet",),
    "transfer": ("transfer_out",),
    "farm": ("farm_buy_cp", "farm_convert"),
    "tags": ("tag_rent",),
    "exchange": ("exchange_escrow",),
    "market": ("market_create_fee",),
}


def _today_msk() -> date:
    return datetime.now(MSK).date()


def _today_msk_start_utc() -> datetime:
    start_msk = datetime.combine(_today_msk(), time.min, tzinfo=MSK)
    return start_msk.astimezone(UTC).replace(tzinfo=None)


async def _today_progress(session: AsyncSession, chat_id: int, user_id: int) -> dict[str, bool]:
    today = _today_msk()
    stat_row = (
        await session.execute(
            select(DailyStat.message_count, DailyStat.photo_count).where(
                DailyStat.chat_id == chat_id,
                DailyStat.user_id == user_id,
                DailyStat.stat_date == today,
            )
        )
    ).first()
    message_count = stat_row.message_count if stat_row is not None else 0
    photo_count = stat_row.photo_count if stat_row is not None else 0

    all_kinds = tuple(k for kinds in _ECONOMY_KIND_QUESTS.values() for k in kinds)
    kind_rows = (
        await session.execute(
            select(EconomyTx.kind, func.count())
            .where(
                EconomyTx.chat_id == chat_id,
                EconomyTx.user_id == user_id,
                EconomyTx.created_at >= _today_msk_start_utc(),
                EconomyTx.kind.in_(all_kinds),
            )
            .group_by(EconomyTx.kind)
        )
    ).all()
    counts_by_kind = {row[0]: row[1] for row in kind_rows}

    def _kind_group_done(quest_key: str) -> bool:
        return sum(counts_by_kind.get(k, 0) for k in _ECONOMY_KIND_QUESTS[quest_key]) >= 1

    return {
        "active": message_count >= 10,
        "photo": photo_count >= 1,
        "social": _kind_group_done("social"),
        "media": _kind_group_done("media"),
        "duel": _kind_group_done("duel"),
        "gacha": _kind_group_done("gacha"),
        "casino": _kind_group_done("casino"),
        "transfer": _kind_group_done("transfer"),
        "farm": _kind_group_done("farm"),
        "tags": _kind_group_done("tags"),
        "exchange": _kind_group_done("exchange"),
        "market": _kind_group_done("market"),
    }


async def get_quest_status(session: AsyncSession, chat_id: int, user_id: int) -> list[dict]:
    today = _today_msk()
    progress = await _today_progress(session, chat_id, user_id)
    claimed_keys = set(
        (
            await session.execute(
                select(QuestCompletion.quest_key).where(
                    QuestCompletion.chat_id == chat_id,
                    QuestCompletion.user_id == user_id,
                    QuestCompletion.day_msk == today,
                )
            )
        )
        .scalars()
        .all()
    )
    return [
        {
            "key": q.key,
            "label": q.label,
            "description": q.description,
            "reward": q.reward,
            "done": progress[q.key],
            "claimed": q.key in claimed_keys,
        }
        for q in QUESTS
    ]


async def claim_quests(session: AsyncSession, chat_id: int, user_id: int) -> dict:
    today = _today_msk()
    progress = await _today_progress(session, chat_id, user_id)

    claimed: list[dict] = []
    total_reward = 0
    for q in QUESTS:
        if not progress[q.key]:
            continue

        stmt = (
            pg_insert(QuestCompletion)
            .values(
                chat_id=chat_id,
                user_id=user_id,
                quest_key=q.key,
                day_msk=today,
                reward_amount=q.reward,
            )
            .on_conflict_do_nothing(
                index_elements=["chat_id", "user_id", "quest_key", "day_msk"]
            )
        )
        result = await session.execute(stmt)
        if result.rowcount == 0:
            continue

        ref_id = f"quest:{chat_id}:{user_id}:{today}:{q.key}"
        await economy_service.credit(
            session, chat_id, user_id, q.reward, kind=f"quest_{q.key}", ref_id=ref_id
        )
        claimed.append({"key": q.key, "label": q.label, "reward": q.reward})
        total_reward += q.reward

    await session.commit()
    return {"day_msk": today, "claimed": claimed, "total_reward": total_reward}


async def get_total_completions(session: AsyncSession, chat_id: int, user_id: int) -> int:
    return (
        await session.execute(
            select(func.count())
            .select_from(QuestCompletion)
            .where(QuestCompletion.chat_id == chat_id, QuestCompletion.user_id == user_id)
        )
    ).scalar_one()
