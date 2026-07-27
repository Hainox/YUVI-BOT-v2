"""Постоянные ачивки (QUEST-02) — 6 one-time-forever наград, в отличие от
ежедневных квестов `bot/services/quests_service.py`: та же pull-модель
(прогресс считается на лету при каждом вызове, ничего не тикает в фоне) и
тот же идемпотентный INSERT-gate паттерн (`on_conflict_do_nothing` как
единственный арбитр "уже выдано или ещё нет"), но БЕЗ дневной компоненты в
ключе уникальности — `uq_achievement_unlock` держит (chat_id, user_id,
achievement_key) без даты, поэтому строка на ачивку пишется РОВНО ОДИН РАЗ
за всю историю пары чат+участник, а не одна в день.

`AchievementUnlock.bonus_levels` — постоянная прибавка к потолку уровней
кликера: сумма по всем разблокированным ачивкам участника скармливается в
`clicker_service.get_effective_max_level` (сам модуль их не считает и не
хранит — только копит и отдаёт сумму через `get_total_bonus_levels`).

`reward_amount`/`bonus_levels` в строке — снапшот каталога `ACHIEVEMENTS` на
момент разблокировки (та же идея, что `EconomyTx.amount` фиксирует сумму на
момент транзакции): если каталог позже перебалансируют, уже выданные бонусы
участников задним числом не меняются.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from bot.services import economy_service
from bot.services import quests_service
from bot.services import stats_service
from common.models.achievement_unlock import AchievementUnlock
from common.models.clicker_farm import ClickerFarm
from common.models.duel import Duel
from common.models.economy_tx import EconomyTx
from common.models.gacha_collection import GachaCollection
from common.models.twin_opt_in import TwinOptIn


@dataclass(frozen=True)
class AchievementDef:
    key: str
    label: str
    description: str
    reward: int
    bonus_levels: int


ACHIEVEMENTS: tuple[AchievementDef, ...] = (
    AchievementDef("streak7", "Неделя огня", "Пиши в чате 7 дней подряд без пропуска.", 100, 3),
    AchievementDef("messages1000", "Болтун", "Напиши в чате 1000 сообщений за всё время.", 150, 3),
    AchievementDef("duels10", "Ветеран дуэлей", "Выиграй 10 дуэлей.", 150, 3),
    AchievementDef("gacha10", "Коллекционер", "Собери 10 разных персонажей в гаче.", 150, 3),
    AchievementDef("media20", "Медиахомяк", "Скачай 20 видео через бота.", 150, 3),
    AchievementDef("quests50", "Прилежный", "Выполни 50 ежедневных квестов за всё время.", 200, 5),
    AchievementDef(
        "casino100", "Завсегдатай казино", "Сыграй в казино 100 раз за всё время.", 200, 5
    ),
    AchievementDef(
        "transfer50",
        "Щедрая душа",
        "Сделай 50 переводов ювиков другим участникам.",
        150,
        3,
    ),
    AchievementDef(
        "farmlvl30",
        "Мастер фермы",
        "Прокачай тап или автокликер фермы до 30 уровня.",
        150,
        3,
    ),
    AchievementDef("exchange10", "Биржевик", "Выставь 10 лотов на бирже.", 150, 3),
    AchievementDef("market5", "Оракул", "Создай 5 рынков предсказаний.", 150, 3),
    AchievementDef(
        "twin_active",
        "Раздвоение личности",
        "Подключи AI-двойника через /twin_optin (или онбординг в мини-аппе).",
        100,
        3,
    ),
)


async def _progress(session: AsyncSession, chat_id: int, user_id: int) -> dict[str, bool]:
    streak = await stats_service.get_streak(session, chat_id, user_id)
    user_stats = await stats_service.get_user_stats(session, chat_id, user_id, days=None)

    duels_won = (
        await session.execute(
            select(func.count())
            .select_from(Duel)
            .where(Duel.chat_id == chat_id, Duel.winner_id == user_id, Duel.status == "resolved")
        )
    ).scalar_one()

    gacha_count = (
        await session.execute(
            select(func.count())
            .select_from(GachaCollection)
            .where(GachaCollection.chat_id == chat_id, GachaCollection.user_id == user_id)
        )
    ).scalar_one()

    media_count = (
        await session.execute(
            select(func.count())
            .select_from(EconomyTx)
            .where(
                EconomyTx.chat_id == chat_id,
                EconomyTx.user_id == user_id,
                EconomyTx.kind == "mediadl_charge",
            )
        )
    ).scalar_one()

    total_quests = await quests_service.get_total_completions(session, chat_id, user_id)

    async def _count_kind(kind: str) -> int:
        return (
            await session.execute(
                select(func.count())
                .select_from(EconomyTx)
                .where(
                    EconomyTx.chat_id == chat_id,
                    EconomyTx.user_id == user_id,
                    EconomyTx.kind == kind,
                )
            )
        ).scalar_one()

    casino_count = await _count_kind("casino_bet")
    transfer_count = await _count_kind("transfer_out")
    exchange_count = await _count_kind("exchange_escrow")
    market_count = await _count_kind("market_create_fee")

    farm_row = (
        await session.execute(
            select(ClickerFarm.tap_level, ClickerFarm.auto_level).where(
                ClickerFarm.chat_id == chat_id, ClickerFarm.user_id == user_id
            )
        )
    ).first()
    farm_level_max = max(farm_row.tap_level, farm_row.auto_level) if farm_row is not None else 0

    twin_status = (
        await session.execute(
            select(TwinOptIn.status).where(
                TwinOptIn.chat_id == chat_id, TwinOptIn.user_id == user_id
            )
        )
    ).scalar_one_or_none()

    return {
        "streak7": streak >= 7,
        "messages1000": user_stats["total_messages"] >= 1000,
        "duels10": duels_won >= 10,
        "gacha10": gacha_count >= 10,
        "media20": media_count >= 20,
        "quests50": total_quests >= 50,
        "casino100": casino_count >= 100,
        "transfer50": transfer_count >= 50,
        "farmlvl30": farm_level_max >= 30,
        "exchange10": exchange_count >= 10,
        "market5": market_count >= 5,
        "twin_active": twin_status == "active",
    }


async def get_achievement_status(session: AsyncSession, chat_id: int, user_id: int) -> list[dict]:
    unlocked_keys = set(
        (
            await session.execute(
                select(AchievementUnlock.achievement_key).where(
                    AchievementUnlock.chat_id == chat_id, AchievementUnlock.user_id == user_id
                )
            )
        )
        .scalars()
        .all()
    )
    return [
        {
            "key": a.key,
            "label": a.label,
            "description": a.description,
            "reward": a.reward,
            "bonus_levels": a.bonus_levels,
            "unlocked": a.key in unlocked_keys,
        }
        for a in ACHIEVEMENTS
    ]


async def claim_achievements(session: AsyncSession, chat_id: int, user_id: int) -> dict:
    progress = await _progress(session, chat_id, user_id)

    unlocked: list[dict] = []
    total_reward = 0
    for a in ACHIEVEMENTS:
        if not progress[a.key]:
            continue

        stmt = (
            pg_insert(AchievementUnlock)
            .values(
                chat_id=chat_id,
                user_id=user_id,
                achievement_key=a.key,
                bonus_levels=a.bonus_levels,
                reward_amount=a.reward,
            )
            .on_conflict_do_nothing(index_elements=["chat_id", "user_id", "achievement_key"])
        )
        result = await session.execute(stmt)
        if result.rowcount == 0:
            continue

        ref_id = f"achievement:{chat_id}:{user_id}:{a.key}"
        await economy_service.credit(
            session, chat_id, user_id, a.reward, kind=f"achievement_{a.key}", ref_id=ref_id
        )
        unlocked.append(
            {"key": a.key, "label": a.label, "reward": a.reward, "bonus_levels": a.bonus_levels}
        )
        total_reward += a.reward

    await session.commit()
    return {"unlocked": unlocked, "total_reward": total_reward}


async def get_total_bonus_levels(session: AsyncSession, chat_id: int, user_id: int) -> int:
    return (
        await session.execute(
            select(func.coalesce(func.sum(AchievementUnlock.bonus_levels), 0)).where(
                AchievementUnlock.chat_id == chat_id, AchievementUnlock.user_id == user_id
            )
        )
    ).scalar_one()
