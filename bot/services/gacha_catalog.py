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
    art_slug: str  # имя ассета /art/heroines/{art_slug}.webp (static Mini App), нумерация из исходного дизайн-пакета, не связана с char_id


_CHARACTERS: tuple[Character, ...] = (
    # --- R (D-07: каталог-only, недостижим через /roll) — стартовый набор ---
    Character("r_elis", "R", "Элис", art_slug="01-elis"),
    Character("r_freya", "R", "Фрея", art_slug="02-freya"),
    Character("r_selin", "R", "Селин", art_slug="03-selin"),
    Character("r_sofia", "R", "София", art_slug="04-sofia"),
    Character("r_nora", "R", "Нора", art_slug="05-nora"),
    # --- S --------------------------------------------------------------------
    Character("s_ignis", "S", "Игнис", art_slug="06-ignis"),
    Character("s_astrid", "S", "Астрид", art_slug="07-astrid"),
    Character("s_amira", "S", "Амира", art_slug="08-amira"),
    Character("s_luna", "S", "Луна", art_slug="09-luna"),
    # --- UR -------------------------------------------------------------------
    Character("ur_iris", "UR", "Айрис", art_slug="10-iris"),
    Character("ur_yuna", "UR", "Юна", art_slug="11-yuna"),
    Character("ur_mia", "UR", "Мия", art_slug="12-mia"),
    # --- UUR (топ-тир, rate-up баннер) -----------------------------------------
    Character("uur_astrea", "UUR", "Астрея", art_slug="13-astrea"),
    Character("uur_eliana", "UUR", "Элиана", art_slug="14-eliana"),
    Character("uur_mara", "UUR", "Мара", art_slug="15-mara"),
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
