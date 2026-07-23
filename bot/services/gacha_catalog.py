"""Каталог персонажей гачи мира Ювитерии (GACHA-01..03, D-03/D-07) — 15
героинь, 4 тира редкости (R/S/UR/UUR), ПОЛНОСТЬЮ в коде, не в БД
(`gacha_collection` хранит только прогресс игрока: stars/copies по char_id,
сам каталог здесь).

Тиры (по редкости, от частого к редкому): S -> UR -> UUR. R — каталог-only,
НЕДОСТИЖИМ через `/roll`/`gacha_service.roll` (существует только для
будущих альтернативных источников — стартовый набор, награды номинаций,
Фаза 5). `TIER_WEIGHTS` ниже сознательно НЕ содержит ключ "R" — не
добавлять его самостоятельно (см. `gacha_service.py` докстринг).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Character:
    """Один персонаж каталога. Доход фермы больше не зависит от отдельного
    поля роли — `clicker_service._collection_income_per_sec` считает доход
    по тиру для ЛЮБОЙ собранной героини (см. её докстринг)."""

    char_id: str
    tier: str  # "R" | "S" | "UR" | "UUR"
    name: str


_CHARACTERS: tuple[Character, ...] = (
    # --- R (D-07: каталог-only, недостижим через /roll) — стартовый набор ---
    Character("r_elis", "R", "Элис"),
    Character("r_freya", "R", "Фрея"),
    Character("r_selin", "R", "Селин"),
    Character("r_sofia", "R", "София"),
    Character("r_nora", "R", "Нора"),
    # --- S --------------------------------------------------------------------
    Character("s_ignis", "S", "Игнис"),
    Character("s_astrid", "S", "Астрид"),
    Character("s_amira", "S", "Амира"),
    Character("s_luna", "S", "Луна"),
    # --- UR -------------------------------------------------------------------
    Character("ur_iris", "UR", "Айрис"),
    Character("ur_yuna", "UR", "Юна"),
    Character("ur_mia", "UR", "Мия"),
    # --- UUR (топ-тир, rate-up баннер) -----------------------------------------
    Character("uur_astrea", "UUR", "Астрея"),
    Character("uur_eliana", "UUR", "Элиана"),
    Character("uur_mara", "UUR", "Мара"),
)

CATALOG: dict[str, Character] = {c.char_id: c for c in _CHARACTERS}

# --- Веса ролла (D-03) — сумма 1.0, БЕЗ ключа "R" (D-07) ---------------------
TIER_WEIGHTS: dict[str, float] = {"S": 0.78, "UR": 0.20, "UUR": 0.02}

PITY_UR = 50  # D-03: порог pity до гарантированного UR-or-better (было PITY_SSR)
PITY_UUR = 90  # D-03: порог pity до гарантированного UUR (сбрасывает оба счётчика, было PITY_UR)

# D-03. UR: 300 -> 400 — конкретный дубль внутри тира подорожал ~в 1.36 раза
# (2->3 персонажа при почти неизменной суммарной вероятности тира, тот же
# метод пересчёта, что и у TIER_WEIGHTS выше). R/S/UUR перенесены 1:1: R
# недостижим через ролл, S — основная масса роллов и уже низкий refund,
# UUR не менял числа персонажей (3->3).
DUPE_REFUND: dict[str, int] = {"R": 20, "S": 80, "UR": 400, "UUR": 1500}
MAX_STARS = 5  # D-03: потолок звёзд от дублей


def chars_of_tier(tier: str) -> list[Character]:
    """Персонажи данного тира, в порядке объявления каталога (стабильный —
    используется гача-роллом для взвешенного/rate-up выбора)."""
    return [c for c in _CHARACTERS if c.tier == tier]


def star_mult(stars: int) -> float:
    """REFERENCE-XYLOZ.md §3.1: множитель дохода фермы от звёзд персонажа
    (1 + 0.25 за каждую звезду сверх первой)."""
    return 1 + 0.25 * (stars - 1)
