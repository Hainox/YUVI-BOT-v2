"""Чистый (pure) движок слота "Azumanga" (04.1-02) — server-authoritative
порт `webapp/slot-data.jsx::randomGrid`/`evaluateGrid` на Python.

Никакого доступа к БД и `economy_service` — этот модуль только считает
сетку/выигрыши по переданным данным (grid/bet_per_line), деньги двигает
`bot.services.casino_service.play_slots` через существующее settle-ядро
04.1-01 (`_settle`/`pay_from_bank`).

`spin_grid(rng)` принимает RNG-объект от вызывающей стороны (в проде — общий
seam `casino_service._rng`, `secrets.SystemRandom()`) — server-authoritative,
никогда клиентский `Math.random()` (D-05/T-04.1-05). Собственный модульный
`_rng` здесь используется ТОЛЬКО для авто-розыгрыша фриспинов внутри
`evaluate_grid` (бонусные спины без дополнительной ставки игрока) — это
внутренняя деталь движка, а не альтернативный источник исхода основной сдачи.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass

from bot.data import slot_data

TOTAL_LINES = len(slot_data.PAYLINES)

# RNG-seam ТОЛЬКО для авто-розыгрыша фриспинов (см. докстринг модуля выше).
_rng = secrets.SystemRandom()


@dataclass
class SlotResult:
    """Итог одной сдачи (включая авто-доигранные фриспины)."""

    grid: list[list[str]]
    line_wins: list[dict]
    scatter_count: int
    freespins: int
    total_payout: int


def _build_reel_strip(col: int) -> list[str]:
    """Взвешенный пул символов для одного столбца (D-05 веса из WEIGHTS +
    лёгкая per-reel вариация, портированная буквально из
    `webapp/slot-data.jsx::buildReelStrip`): scatter (keffiyeh) получает +1
    веса на крайних столбцах (0 и 4), wild (muscle) — +1 на среднем (2).

    Эта вариация НЕ "визуальная мелочь" — она часть модели, на которой
    Monte-Carlo калибровался паутейбл D-06 (RTP ~92.78%, `04-CONTEXT.md`).
    Без неё (единый плоский пул на все 5 столбцов) реальный сэмплированный
    RTP падает до ~0.81 — ниже допустимого диапазона [0.88, 0.97]
    (обнаружено `test_sampled_rtp_in_band` при первом прогоне GREEN, см.
    SUMMARY, деталь исправления Rule 1). Детерминированный shuffle черновика
    (визуальный порядок символов на барабане) НЕ портируется — не влияет на
    вероятности при равномерном `rng.choice(...)` по всему пулу."""
    strip: list[str] = []
    for symbol_id, weight in slot_data.WEIGHTS.items():
        w = weight
        role = slot_data.SYMBOLS[symbol_id]["role"]
        if role == "scatter" and col in (0, 4):
            w += 1
        if role == "wild" and col == 2:
            w += 1
        strip.extend([symbol_id] * w)
    return strip


_REEL_STRIPS = [_build_reel_strip(col) for col in range(5)]


def spin_grid(rng) -> list[list[str]]:
    """Сетка 3 строки x 5 столбцов: каждая ячейка столбца `col` — независимый
    взвешенный выбор `rng.choice(...)` из пула ИМЕННО этого столбца
    (`_REEL_STRIPS[col]`, D-05 per-reel вариация)."""
    grid: list[list[str]] = [[] for _ in range(3)]
    for col in range(5):
        for row in range(3):
            grid[row].append(rng.choice(_REEL_STRIPS[col]))
    return grid


def _line_payout(on_line: list[str], bet_per_line: int) -> dict | None:
    """Определяет исход одной payline: leftmost non-wild как target (wild
    подставляется вместо любого символа кроме scatter), подсчёт от левого
    края, минимум 3 в ряд."""
    target = None
    for symbol in on_line:
        if symbol != slot_data.WILD_ID:
            target = symbol
            break
    if target is None:
        target = slot_data.WILD_ID  # все ячейки — wild
    if target == slot_data.SCATTER_ID:
        return None  # scatter никогда не платит по линиям

    count = 0
    for symbol in on_line:
        if symbol == target or symbol == slot_data.WILD_ID:
            count += 1
        else:
            break
    if count < 3:
        return None

    payout = slot_data.PAYTABLE.get(target, {}).get(count, 0) * bet_per_line
    if payout <= 0:
        return None
    return {"symbol": target, "count": count, "payout": payout}


def _sum_line_wins(grid: list[list[str]], bet_per_line: int) -> tuple[list[dict], int]:
    line_wins: list[dict] = []
    total = 0
    for line_idx, line in enumerate(slot_data.PAYLINES):
        on_line = [grid[row][col] for col, row in enumerate(line)]
        win = _line_payout(on_line, bet_per_line)
        if win is None:
            continue
        win = {"line_index": line_idx, **win}
        line_wins.append(win)
        total += win["payout"]
    return line_wins, total


def _count_scatter(grid: list[list[str]]) -> int:
    return sum(1 for row in grid for symbol in row if symbol == slot_data.SCATTER_ID)


def _freespins_for(scatter_count: int) -> int:
    """>=3 keffiyeh в любом месте сетки -> фриспины (D-05: 3=4, 4=6, 5+=7)."""
    if scatter_count < 3:
        return 0
    if scatter_count == 3:
        return slot_data.FREESPIN_TABLE[3]
    if scatter_count == 4:
        return slot_data.FREESPIN_TABLE[4]
    return slot_data.FREESPIN_TABLE[5]


def evaluate_grid(grid: list[list[str]], bet_per_line: int) -> SlotResult:
    """Считает линии + scatter/freespins для сдачи `grid`, затем автоматически
    доигрывает `freespins` дополнительных бонусных спинов на том же
    `bet_per_line` (без ставки игрока) и суммирует их линейные выигрыши в
    `total_payout` — сервер сам "проигрывает" фриспины одним расчётом
    (никакого повторного ретриггера фриспинов внутри бонусных спинов, чтобы
    исключить неограниченную рекурсию)."""
    line_wins, line_total = _sum_line_wins(grid, bet_per_line)
    scatter_count = _count_scatter(grid)
    freespins = _freespins_for(scatter_count)

    total_payout = line_total
    for _ in range(freespins):
        fs_grid = spin_grid(_rng)
        _fs_wins, fs_total = _sum_line_wins(fs_grid, bet_per_line)
        total_payout += fs_total

    return SlotResult(
        grid=grid,
        line_wins=line_wins,
        scatter_count=scatter_count,
        freespins=freespins,
        total_payout=total_payout,
    )
