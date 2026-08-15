from __future__ import annotations

from dataclasses import dataclass

from common.arena.schemas import FighterType


@dataclass(frozen=True, slots=True)
class FighterDefinition:
    fighter_type: FighterType
    role: str
    max_hp: int
    fast_damage: int
    heavy_damage: int
    special_name: str
    special_kind: str
    special_damage: int = 0


FIGHTERS: dict[FighterType, FighterDefinition] = {
    FighterType.TANK: FighterDefinition(
        fighter_type=FighterType.TANK,
        role="Выносливость и защита",
        max_hp=130,
        fast_damage=8,
        heavy_damage=20,
        special_name="Крепость",
        special_kind="fortress",
    ),
    FighterType.ASSASSIN: FighterDefinition(
        fighter_type=FighterType.ASSASSIN,
        role="Скорость и уклонение",
        max_hp=80,
        fast_damage=14,
        heavy_damage=17,
        special_name="Молниеносный рывок",
        special_kind="dash",
        special_damage=30,
    ),
    FighterType.BERSERKER: FighterDefinition(
        fighter_type=FighterType.BERSERKER,
        role="Риск и урон при низком HP",
        max_hp=105,
        fast_damage=11,
        heavy_damage=24,
        special_name="Ярость",
        special_kind="rage",
    ),
    FighterType.TACTICIAN: FighterDefinition(
        fighter_type=FighterType.TACTICIAN,
        role="Контроль и эффекты",
        max_hp=95,
        fast_damage=10,
        heavy_damage=18,
        special_name="Тактическая ловушка",
        special_kind="trap",
    ),
}
