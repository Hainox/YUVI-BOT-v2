"""Payline-оценка и тумбл-каскад слота "Тето Брейнрот" — порт механики Hades
Gigablox поверх мегаблоков (`teto_megablock.py`), не отдельных клеток.

Тумбл (в отличие от `slot_engine.py`, где сдача — один статичный спин):
выигравший мегаблок удаляется ЦЕЛИКОМ, оставшиеся блоки гравитируют вниз как
RIGID BODY (блок падает ЕДИНЫМ целым — соседние столбцы под широким блоком
должны быть свободны по ВСЕЙ его ширине, наивная per-column гравитация
разорвала бы блок на части), затем `teto_megablock.form_blocks` добирает
освободившиеся клетки. Повторяется, пока есть выигрыш — хардкап на число
повторов см. `teto_slot_engine.TUMBLE_HARD_CAP`, не в этом модуле."""

from __future__ import annotations

from dataclasses import replace

from bot.services.teto_megablock import ALL_CELLS
from bot.services.teto_megablock import GRID_SIZE
from bot.services.teto_megablock import SCATTER_ID
from bot.services.teto_megablock import WILD_ID
from bot.services.teto_megablock import MegaBlock
from bot.services.teto_megablock import form_blocks

# 50 фиксированных paylines слева направо на сетке 6x6 (roadmap.md: перенос
# геометрии оригинала 1:1). MVP: 3 репрезентативные линии (горизонталь-центр,
# диагональ вниз, диагональ вверх) — полный набор 50 линий калибруется вместе
# с паутейблом на Monte-Carlo прогоне (см. roadmap.md), логика оценки ниже уже
# работает на ЛЮБОМ наборе линий длины `GRID_SIZE`.
TETO_PAYLINES: list[list[int]] = [
    [2, 2, 2, 2, 2, 2],
    [0, 1, 2, 3, 4, 5],
    [5, 4, 3, 2, 1, 0],
]


def evaluate_paylines(blocks: list[MegaBlock]) -> tuple[bool, set[int]]:
    """Leftmost non-wild символ на линии — target; подряд слева направо,
    `WILD_ID` считается за любой символ кроме `SCATTER_ID`; 3+ подряд
    совпадений — выигрыш. Скаттер как target пропускается (платит по
    количеству блоков на экране — `count_scatter_blocks` — а не по payline)."""
    cell_to_block: dict[tuple[int, int], MegaBlock] = {}
    for b in blocks:
        for cell in b.cells:
            cell_to_block[cell] = b

    any_win = False
    winning_ids: set[int] = set()

    for line in TETO_PAYLINES:
        line_blocks = [cell_to_block[(line[col], col)] for col in range(GRID_SIZE)]
        symbols = [blk.symbol_id for blk in line_blocks]
        target = next((s for s in symbols if s != WILD_ID), WILD_ID)

        if target == SCATTER_ID:
            continue

        matched_ids: list[int] = []
        for blk, sym in zip(line_blocks, symbols):
            if sym == target or sym == WILD_ID:
                matched_ids.append(blk.block_id)
            else:
                break

        if len(matched_ids) >= 3:
            any_win = True
            winning_ids.update(matched_ids)

    return any_win, winning_ids


def count_scatter_blocks(blocks: list[MegaBlock]) -> int:
    """Число SCATTER-БЛОКОВ на доске (не клеток) — ЗАФИКСИРОВАНО владельцем
    бота 2026-07-30: скаттер-мегаблок — один крупный символ, растянутый на
    несколько клеток (до 16, `MEGA_BLOCK_SCATTER_AREA_CAP`), а не N отдельных
    скаттеров, поэтому формула фриспинов (`teto_slot_engine.
    compute_freespins_awarded`) считает БЛОКИ, а не клетки."""
    return sum(1 for b in blocks if b.symbol_id == SCATTER_ID)


def resolve_tumble(rng, blocks: list[MegaBlock], winning_block_ids: set[int]) -> list[MegaBlock]:
    """Убирает блоки с id в `winning_block_ids` целиком, гравитирует
    оставшиеся блоки rigid-body (см. докстринг модуля), затем доборает
    освободившиеся клетки через `form_blocks`."""
    survivors = [b for b in blocks if b.block_id not in winning_block_ids]

    order = sorted(survivors, key=lambda b: -(b.row + b.height))
    settled: list[MegaBlock] = []
    occupied: set[tuple[int, int]] = set()

    for b in order:
        new_row = b.row
        while True:
            candidate_row = new_row + 1
            if candidate_row + b.height > GRID_SIZE:
                break
            candidate_cells = {
                (r, c)
                for r in range(candidate_row, candidate_row + b.height)
                for c in range(b.col, b.col + b.width)
            }
            if candidate_cells & occupied:
                break
            new_row = candidate_row
        moved = b if new_row == b.row else replace(b, row=new_row)
        settled.append(moved)
        occupied |= set(moved.cells)

    empty_cells = ALL_CELLS - occupied
    # block_id для добора продолжается ПОСЛЕ максимума среди ВСЕХ блоков
    # (включая уже удалённые winning_block_ids) — не только среди survivors —
    # иначе новый блок на освободившейся клетке мог бы получить тот же id,
    # что уже удалённый выигравший блок этого же тумбл-шага (коллизия id,
    # найдена и отсеяна на прототипе, см. roadmap.md/отчёт по Тето).
    next_id = max((b.block_id for b in blocks), default=-1) + 1
    new_blocks = form_blocks(rng, empty_cells, start_id=next_id)

    return settled + new_blocks
