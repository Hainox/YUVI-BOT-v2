"""Каталог персонажей гачи (GACHA-01..03, D-03/D-07) — ПОЛНОСТЬЮ в коде, не в
БД (`gacha_collection` хранит только прогресс игрока: stars/copies по
char_id, сам каталог здесь).

D-07: каталог — 4 тира редкости (R/SR/SSR/UR), как в эталоне xyloz_tg_bot.
`TIER_WEIGHTS` ниже сознательно НЕ содержит ключ "R" — R-персонажи существуют
только для будущих альтернативных источников (стартовый набор, награды
номинаций — Фаза 5), НЕДОСТИЖИМЫ через `/roll`/`gacha_service.roll`. Не
добавлять пятый вес для R самостоятельно (см. `gacha_service.py` докстринг).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Character:
    """Один персонаж каталога. `role` влияет на формулу дохода фермы
    (`clicker_service`, worker/heroine — REFERENCE-XYLOZ.md §3.1), сама
    формула переносится в план фермы, здесь только декларация роли."""

    char_id: str
    tier: str  # "R" | "SR" | "SSR" | "UR"
    role: str  # "worker" | "heroine"
    name: str


_CHARACTERS: tuple[Character, ...] = (
    # --- R (D-07: каталог-only, недостижим через /roll) ---------------------
    Character("r_001", "R", "worker", "Стажёр Вова"),
    Character("r_002", "R", "heroine", "Соседка Люся"),
    # --- SR ------------------------------------------------------------------
    Character("sr_001", "SR", "worker", "Дядя Толя с гаражами"),
    Character("sr_002", "SR", "heroine", "Пельмень-тян"),
    Character("sr_003", "SR", "worker", "Дед Watchdog"),
    # --- SSR -------------------------------------------------------------------
    Character("ssr_001", "SSR", "heroine", "Царица Ювиков"),
    Character("ssr_002", "SSR", "worker", "Легендарный Санёк"),
    # --- UR ------------------------------------------------------------------
    Character("ur_001", "UR", "heroine", "Богиня Банка Чата"),
    Character("ur_002", "UR", "worker", "Древний Модератор"),
    Character("ur_003", "UR", "heroine", "Юви Президент"),
)

CATALOG: dict[str, Character] = {c.char_id: c for c in _CHARACTERS}

# --- Веса ролла (D-03) — сумма 1.0, БЕЗ ключа "R" (D-07) ---------------------
TIER_WEIGHTS: dict[str, float] = {"SR": 0.80, "SSR": 0.18, "UR": 0.02}

PITY_SSR = 50  # D-03: порог pity до гарантированного SSR-or-better
PITY_UR = 90  # D-03: порог pity до гарантированного UR (сбрасывает оба счётчика)

DUPE_REFUND: dict[str, int] = {"R": 20, "SR": 80, "SSR": 300, "UR": 1500}  # D-03
MAX_STARS = 5  # D-03: потолок звёзд от дублей


def chars_of_tier(tier: str) -> list[Character]:
    """Персонажи данного тира, в порядке объявления каталога (стабильный —
    используется гача-роллом для взвешенного/rate-up выбора)."""
    return [c for c in _CHARACTERS if c.tier == tier]


def star_mult(stars: int) -> float:
    """REFERENCE-XYLOZ.md §3.1: множитель дохода фермы от звёзд персонажа
    (1 + 0.25 за каждую звезду сверх первой)."""
    return 1 + 0.25 * (stars - 1)
