"""Юнит-тесты `constellation_catalog.py` — чистого data-модуля без побочных
эффектов (см. докстринг модуля: без БД, без импортов кроме
`dataclasses`/`typing`). Поэтому это ПЛОСКИЕ pytest-тесты — никакой
async-фикстуры `session`, никакого `@pytest.mark.asyncio`: сам модуль и все
его функции (`const_level`, `sum_effect`, `bonuses_for`) синхронные и не
трогают БД.

Покрывает: клэмпинг уровня созвездия по числу дублей, стекинг одинаковых
`effect` на разных уровнях одного персонажа, отсутствие эффекта у
персонажа без нужной записи, безопасный пропуск None-value записей
(daily freebie / UX-заметки), сортировку `bonuses_for` и целостность
таблицы `CONSTELLATIONS` (общее число строк и присутствие всех 15
персонажей гачи)."""

from __future__ import annotations

import pytest

from bot.services import constellation_catalog
from bot.services.constellation_catalog import (
    DAILY_FREEBIE,
    DUEL_LOSS_REFUND_PCT,
    FARM_CP_PCT,
    bonuses_for,
    const_level,
    sum_effect,
)

# Все 15 реальных char_id персонажей гачи (см. gacha_catalog.py) — каждый
# должен встречаться в CONSTELLATIONS хотя бы раз.
ALL_CHAR_IDS = (
    "r_elis",
    "r_freya",
    "r_selin",
    "r_sofia",
    "r_nora",
    "s_ignis",
    "s_astrid",
    "s_amira",
    "s_luna",
    "ur_iris",
    "ur_yuna",
    "ur_mia",
    "uur_astrea",
    "uur_eliana",
    "uur_mara",
)


class TestConstLevel:
    """`const_level(copies)` = `max(0, min(6, copies - 1))` — 1-я копия
    ещё не даёт уровня созвездия (C0), уровень растёт с каждым дублем и
    клэмпится сверху на 6 (максимум C6)."""

    def test_first_copy_is_level_zero(self) -> None:
        assert const_level(1) == 0

    def test_second_copy_is_level_one(self) -> None:
        assert const_level(2) == 1

    def test_seventh_copy_is_level_six(self) -> None:
        assert const_level(7) == 6

    def test_level_clamped_at_six_for_many_copies(self) -> None:
        assert const_level(100) == 6

    def test_zero_copies_defensive_edge_case_stays_zero(self) -> None:
        # copies=0 не должно случаться в реальных данных (GachaCollection
        # создаётся только при первом дубле), но функция не должна уйти в
        # отрицательный уровень при таком защитном вызове.
        assert const_level(0) == 0


class TestSumEffectFarmCp:
    """`sum_effect` на реальной героине r_elis для FARM_CP_PCT — у неё
    единственная запись farm_cp_pct на уровне C1 (+2%), выше по уровню
    новых farm_cp_pct узлов нет, поэтому сумма не должна расти дальше."""

    def test_level_zero_gives_no_bonus(self) -> None:
        assert sum_effect("r_elis", 0, FARM_CP_PCT) == 0.0

    def test_level_one_unlocks_c1_farm_bonus(self) -> None:
        assert sum_effect("r_elis", 1, FARM_CP_PCT) == pytest.approx(0.02)

    def test_level_six_stays_at_c1_value_no_double_counting(self) -> None:
        assert sum_effect("r_elis", 6, FARM_CP_PCT) == pytest.approx(0.02)


class TestSumEffectStacking:
    """r_sofia имеет duel_loss_refund_pct дважды: C2=3% и C5=5%. Оба узла
    должны СТЕКАТЬСЯ (складываться), а не последний перезаписывать
    предыдущий — так задокументировано в докстринге sum_effect."""

    def test_only_c2_node_unlocked_at_level_two(self) -> None:
        assert sum_effect("r_sofia", 2, DUEL_LOSS_REFUND_PCT) == pytest.approx(0.03)

    def test_c2_and_c5_nodes_stack_at_level_five(self) -> None:
        assert sum_effect("r_sofia", 5, DUEL_LOSS_REFUND_PCT) == pytest.approx(0.08)


class TestSumEffectMissingEntry:
    """r_selin не имеет ни одной записи farm_cp_pct в каталоге — сумма
    должна быть нулевой на любом уровне, включая максимальный C6."""

    def test_no_farm_cp_entry_returns_zero_even_at_max_level(self) -> None:
        assert sum_effect("r_selin", 6, FARM_CP_PCT) == 0.0


class TestSumEffectNoneValues:
    """Записи со value=None (daily freebie / чисто UX-подсказки) не
    участвуют в сумме и не должны приводить к ошибке при попытке их
    сложить — фильтр `value is not None` в sum_effect обязан их отсеять
    до суммирования."""

    def test_daily_freebie_entries_are_ignored_not_summed(self) -> None:
        assert sum_effect("r_elis", 6, DAILY_FREEBIE) == 0.0

    def test_daily_freebie_never_raises(self) -> None:
        # Регрессия: sum() над None упал бы с TypeError, если бы фильтр
        # "value is not None" отсутствовал или был сломан.
        try:
            sum_effect("r_elis", 6, DAILY_FREEBIE)
        except TypeError:
            pytest.fail("sum_effect не должен падать на None-value записях")


class TestBonusesFor:
    """`bonuses_for` возвращает разблокированные узлы персонажа (level <=
    заданного), отсортированные по level по возрастанию — для отображения
    в UI codex-экрана."""

    def test_level_three_returns_three_bonuses_sorted_ascending(self) -> None:
        bonuses = bonuses_for("r_elis", 3)
        assert len(bonuses) == 3
        assert [b.level for b in bonuses] == [1, 2, 3]

    def test_level_zero_returns_empty_list(self) -> None:
        assert bonuses_for("r_elis", 0) == []


class TestCatalogIntegrity:
    """Целостность всей таблицы CONSTELLATIONS: 15 героинь x 6 уровней =
    90 узлов дизайн-документа, но uur_astrea C4 разложена на 2 отдельные
    записи ConstellationBonus (см. докстринг модуля) => 91 строка."""

    def test_total_row_count_is_91(self) -> None:
        assert len(constellation_catalog.CONSTELLATIONS) == 91

    @pytest.mark.parametrize("char_id", ALL_CHAR_IDS)
    def test_every_real_char_id_present_in_catalog(self, char_id: str) -> None:
        assert any(c.char_id == char_id for c in constellation_catalog.CONSTELLATIONS)
