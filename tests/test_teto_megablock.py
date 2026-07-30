"""Тесты сетки/мегаблоков слота "Тето Брейнрот" (`bot/services/teto_megablock.py`).
Plain pytest, без async/БД-фикстур — чистые функции, тот же принцип, что и
`tests/test_slot_engine.py`, только этот модуль вообще не трогает `casino_service`.

Доказывают:
- `assert_valid_partition` — полная партиция 6x6 (`sum(area)==36`, без
  пропусков/наложений) держится и на полном заполнении, и на частичном
  добор-регионе (после тумбла).
- `form_blocks`: MVP не более ОДНОГО настоящего мегаблока (area>=4) за вызов;
  fallback на одиночные 1x1, если ролл не сработал ИЛИ форма не вписывается в
  переданный (в т.ч. "рваный", не прямоугольный) регион; скаттер-мегаблок с
  area>16 никогда не размещается (`MEGA_BLOCK_SCATTER_AREA_CAP`).
- `WILD_ID` никогда не входит в `ALL_SYMBOLS` — не может появиться из
  обычного заполнения реела (появляется только через Дрель-Хант, см.
  `test_teto_drillhunt.py`)."""

from __future__ import annotations

import random

from bot.services.teto_megablock import ALL_CELLS
from bot.services.teto_megablock import ALL_SYMBOLS
from bot.services.teto_megablock import SCATTER_ID
from bot.services.teto_megablock import WILD_ID
from bot.services.teto_megablock import MegaBlock
from bot.services.teto_megablock import assert_valid_partition
from bot.services.teto_megablock import form_blocks


class _ForcedRng:
    """`.choice(seq)` — следующий элемент из заданной последовательности по
    кругу (игнорирует `seq`). `.randint(a, b)` — следующее целое из заданной
    последовательности (по кругу), либо `b`, если исчерпано. Форма стаба —
    буквально из ТЗ Тето-прототипа (см. roadmap.md)."""

    def __init__(self, choices=(), randints=()):
        self._choices = list(choices)
        self._randints = list(randints)
        self._ci = 0
        self._ri = 0

    def choice(self, seq):
        if not self._choices:
            return seq[0]
        v = self._choices[self._ci % len(self._choices)]
        self._ci += 1
        return v

    def randint(self, a, b):
        if not self._randints:
            return b
        v = self._randints[self._ri % len(self._randints)]
        self._ri += 1
        return v


def assert_valid_partition_over(blocks: list[MegaBlock], expected_cells: set[tuple[int, int]]) -> None:
    """Тот же инвариант, что `assert_valid_partition`, но над ПРОИЗВОЛЬНЫМ
    регионом (не обязательно всей доской 6x6) — нужен для тестов частичного
    добора (`form_blocks(rng, partial_region)`)."""
    seen: set[tuple[int, int]] = set()
    for b in blocks:
        for cell in b.cells:
            assert cell not in seen, f"клетка {cell} покрыта дважды"
            seen.add(cell)
    assert seen == expected_cells, f"покрытие не совпадает с ожидаемым регионом: {expected_cells ^ seen}"
    assert sum(b.area for b in blocks) == len(expected_cells)


def test_megablock_cells_and_area_cover_expected_rectangle():
    b = MegaBlock(block_id=0, symbol_id="teto_chimera", row=1, col=2, height=2, width=3)
    assert b.area == 6
    assert set(b.cells) == {(1, 2), (1, 3), (1, 4), (2, 2), (2, 3), (2, 4)}


def test_assert_valid_partition_passes_on_full_board_singles():
    blocks = [
        MegaBlock(block_id=i, symbol_id="utau_note", row=r, col=c, height=1, width=1)
        for i, (r, c) in enumerate(sorted(ALL_CELLS))
    ]
    assert_valid_partition(blocks)  # не должно поднять AssertionError


def test_assert_valid_partition_catches_overlap():
    blocks = [
        MegaBlock(block_id=0, symbol_id="utau_note", row=0, col=0, height=2, width=2),
        MegaBlock(block_id=1, symbol_id="utau_note", row=1, col=1, height=1, width=1),  # пересекается с block 0
    ]
    try:
        assert_valid_partition(blocks)
    except AssertionError:
        return
    raise AssertionError("assert_valid_partition должен был поймать наложение клеток")


def test_form_blocks_full_board_fallback_all_singles_partition_invariant():
    # randints=[5] -> rng.randint(1, 10) == 1 ложно (5 != 1) -> обычный fallback.
    rng = _ForcedRng(choices=ALL_SYMBOLS, randints=[5])
    blocks = form_blocks(rng, set(ALL_CELLS))
    assert_valid_partition(blocks)
    assert all(b.area == 1 for b in blocks), "fallback-путь обязан давать только одиночные 1x1"


def test_form_blocks_mega_block_placed_when_forced_and_fits():
    # randints: [1 -> попытка мегаблока сработала, 0 -> top, 0 -> left].
    rng = _ForcedRng(choices=[(3, 3), "teto_chimera", "baguette_crumb"], randints=[1, 0, 0])
    blocks = form_blocks(rng, set(ALL_CELLS))
    assert_valid_partition(blocks)
    megas = [b for b in blocks if b.area >= 4]
    assert len(megas) == 1, "MVP допускает не более ОДНОГО настоящего мегаблока за вызов"
    assert (megas[0].height, megas[0].width) == (3, 3)
    assert (megas[0].row, megas[0].col) == (0, 0)
    assert megas[0].symbol_id == "teto_chimera"


def test_form_blocks_mega_attempt_but_shape_does_not_fit_falls_back_to_all_singles():
    # "Рваный" (не прямоугольный) регион — мегаблок 3x3 в него никогда не
    # впишется целиком, form_blocks обязан откатиться к одиночным клеткам,
    # сохранив партицию ИМЕННО этого региона (не всей доски).
    jagged = {(0, 0), (0, 1), (1, 0), (2, 4), (2, 5), (5, 5)}
    rng = _ForcedRng(choices=[(3, 3), "teto_chimera"], randints=[1, 0, 0])
    blocks = form_blocks(rng, jagged)
    assert_valid_partition_over(blocks, jagged)
    assert all(b.area == 1 for b in blocks)


def test_form_blocks_partial_refill_region_partition_invariant():
    # Имитация пост-тумбл случая: только разбросанное подмножество клеток пусто.
    partial = {(0, 0), (0, 5), (3, 2), (3, 3), (5, 0), (5, 1), (5, 2)}
    rng = _ForcedRng(choices=ALL_SYMBOLS, randints=[7])
    blocks = form_blocks(rng, partial)
    assert_valid_partition_over(blocks, partial)


def test_scatter_mega_block_area_over_16_is_never_placed():
    """1000 случайных сидов — `form_blocks` строит кандидатов символа для
    мегаблока (`LOW_SYMBOLS + HIGH_SYMBOLS`, + `SCATTER_ID` ТОЛЬКО если
    `area <= MEGA_BLOCK_SCATTER_AREA_CAP`) ДО вызова `rng.choice(...)` — с
    реальным RNG скаттер физически не может быть выбран, если area>16, потому
    что его нет в кандидатах. Это стресс-тест на реальном `random.Random`, а
    не форсированный: ручной `_ForcedRng` не валидирует, что форсируемое
    значение реально входит в переданный `seq`, поэтому не может честно
    проверить именно ФИЛЬТРАЦИЮ кандидатов (см. находку про ForcedRng,
    roadmap.md/отчёт по Тето)."""
    for seed in range(1000):
        rng = random.Random(seed)
        blocks = form_blocks(rng, set(ALL_CELLS))
        oversized_scatter = [b for b in blocks if b.symbol_id == SCATTER_ID and b.area > 16]
        assert oversized_scatter == [], f"seed={seed}: переразмеренный скаттер-мегаблок размещён"


def test_wild_symbol_never_appears_in_form_blocks_output():
    """1000 случайных сидов — `WILD_ID` не должен встретиться НИ РАЗУ в
    обычном заполнении (появляется только через Дрель-Хант, см.
    `test_teto_drillhunt.py::test_apply_drill_hunt_*`)."""
    for seed in range(1000):
        rng = random.Random(seed)
        blocks = form_blocks(rng, set(ALL_CELLS))
        for b in blocks:
            assert b.symbol_id != WILD_ID, f"seed={seed}: WILD_ID найден в обычном заполнении"
