"""Payline-оценка и тумбл-каскад слота "Тето Брейнрот" — порт механики Hades
Gigablox поверх мегаблоков (`teto_megablock.py`), не отдельных клеток.

Тумбл (в отличие от `slot_engine.py`, где сдача — один статичный спин):
выигравший мегаблок удаляется ЦЕЛИКОМ, оставшиеся блоки гравитируют вниз как
RIGID BODY (блок падает ЕДИНЫМ целым — соседние столбцы под широким блоком
должны быть свободны по ВСЕЙ его ширине, наивная per-column гравитация
разорвала бы блок на части), затем `teto_megablock.form_blocks` добирает
освободившиеся клетки. Повторяется, пока есть выигрыш — хардкап на число
повторов см. `teto_slot_engine.TUMBLE_HARD_CAP`, не в этом модуле.

`evaluate_paylines` отвечает на вопрос "есть ли выигрыш и какие блоки убрать"
(этого достаточно деньгам); `describe_winning_lines` — чистый сиблинг, который
отвечает "как именно нарисовать каждую выигравшую линию" (этого требует
анимация миниаппа, см. `teto_slot_engine.serialize_animation`). Разделение
намеренное и завязано на арность call site'а в движке — см. её докстринг."""

from __future__ import annotations

from dataclasses import replace

from bot.services.teto_megablock import ALL_CELLS
from bot.services.teto_megablock import GRID_SIZE
from bot.services.teto_megablock import SCATTER_ID
from bot.services.teto_megablock import WILD_ID
from bot.services.teto_megablock import MegaBlock
from bot.services.teto_megablock import form_blocks

# 50 фиксированных paylines слева направо на сетке 6x6 (roadmap.md: перенос
# геометрии оригинала 1:1 — ровно 50, как в Hades Gigablox). Полный набор
# вместо прежнего 3-линейного MVP; сборка по семействам, чтобы покрытие было
# равномерным по строкам, а каждая линия — визуально осмысленной фигурой (это
# важно оверлею линий в миниаппе, а не только математике):
#   6  горизонталей, 2 полные диагонали,
#   10 зигзагов по соседним строкам, 4 V + 4 Λ (глубина 2),
#   8  «лесенок» (вниз/вверх), 5 U + 5 ∩ (глубина 1),
#   4  широких V/Λ с шагом 2, 2 «бугорка».
# Оценка ниже работает на любом наборе линий длины `GRID_SIZE`; уникальность,
# длину и диапазон строк пинует tests/test_teto_tumble.py.
TETO_PAYLINES: list[list[int]] = [
    # горизонтали
    [0, 0, 0, 0, 0, 0],
    [1, 1, 1, 1, 1, 1],
    [2, 2, 2, 2, 2, 2],
    [3, 3, 3, 3, 3, 3],
    [4, 4, 4, 4, 4, 4],
    [5, 5, 5, 5, 5, 5],
    # полные диагонали
    [0, 1, 2, 3, 4, 5],
    [5, 4, 3, 2, 1, 0],
    # зигзаги по соседним строкам
    [0, 1, 0, 1, 0, 1],
    [1, 0, 1, 0, 1, 0],
    [1, 2, 1, 2, 1, 2],
    [2, 1, 2, 1, 2, 1],
    [2, 3, 2, 3, 2, 3],
    [3, 2, 3, 2, 3, 2],
    [3, 4, 3, 4, 3, 4],
    [4, 3, 4, 3, 4, 3],
    [4, 5, 4, 5, 4, 5],
    [5, 4, 5, 4, 5, 4],
    # V (вниз-вверх, глубина 2)
    [2, 1, 0, 0, 1, 2],
    [3, 2, 1, 1, 2, 3],
    [4, 3, 2, 2, 3, 4],
    [5, 4, 3, 3, 4, 5],
    # Λ (вверх-вниз, глубина 2)
    [0, 1, 2, 2, 1, 0],
    [1, 2, 3, 3, 2, 1],
    [2, 3, 4, 4, 3, 2],
    [3, 4, 5, 5, 4, 3],
    # лесенки вниз
    [0, 0, 1, 1, 2, 2],
    [1, 1, 2, 2, 3, 3],
    [2, 2, 3, 3, 4, 4],
    [3, 3, 4, 4, 5, 5],
    # лесенки вверх
    [2, 2, 1, 1, 0, 0],
    [3, 3, 2, 2, 1, 1],
    [4, 4, 3, 3, 2, 2],
    [5, 5, 4, 4, 3, 3],
    # U (глубина 1)
    [1, 0, 0, 0, 0, 1],
    [2, 1, 1, 1, 1, 2],
    [3, 2, 2, 2, 2, 3],
    [4, 3, 3, 3, 3, 4],
    [5, 4, 4, 4, 4, 5],
    # ∩ (глубина 1)
    [0, 1, 1, 1, 1, 0],
    [1, 2, 2, 2, 2, 1],
    [2, 3, 3, 3, 3, 2],
    [3, 4, 4, 4, 4, 3],
    [4, 5, 5, 5, 5, 4],
    # широкие V/Λ с шагом 2
    [0, 2, 4, 4, 2, 0],
    [5, 3, 1, 1, 3, 5],
    [1, 3, 5, 5, 3, 1],
    [4, 2, 0, 0, 2, 4],
    # бугорки
    [2, 2, 3, 3, 2, 2],
    [3, 3, 2, 2, 3, 3],
]

# Паутейбл: множитель ЗА КЛЕТКУ выигравшего блока, в ювиках на единицу
# `bet_per_line`. Выплата шага каскада = bet_per_line * sum(мультипликатор *
# площадь) по выигравшим блокам — «крупный блок платит крупно» (суть Gigablox)
# сохраняется, но high-символы теперь реально дороже low (до этого паутейбла
# выплата была символо-агностичной: любой блок платил 1/клетку, и деление на
# 4 low / 4 high / топ существовало только на бумаге).
#
# ЦЕЛЫЕ множители намеренно: базовая частота выигрышей на 50 линиях достаточно
# низка (RTP на единичном паутейбле ~0.10 при откалиброванных WEIGHTS), так что
# целочисленных шагов хватает для точной калибровки, а дробные значения
# потребовали бы деления с округлением — источник расхождения между суммой
# пошаговых выплат трейса и раундовой выплатой (инвариант «сумма
# evaluate.payout == round_end.raw_round_payout» держится тестом и обязан
# сходиться ТОЧНО, floor его сломал бы).
#
# Значения откалиброваны Monte-Carlo прогоном scripts/calibrate_teto_rtp.py
# (цель RTP 96.00%, перенос 1:1 с оригинала, см. roadmap.md) — НЕ менять без
# повторного прогона: прецедент D-06 (сломанный RTP ~38-40% на непроверенных
# черновых цифрах) уже был.
#
# Wild (golden_drill) платит как топ-символ: он появляется ТОЛЬКО через
# Дрель-Хант (вес в заполнении 0), и его ценность — часть механики момента.
# Скаттера в таблице НЕТ сознательно: скаттер-блок структурно не может войти
# в линейный выигрыш (target пропускает скаттер, wild за скаттер не считается
# — см. evaluate_paylines), поэтому запись для него была бы мёртвой строкой,
# маскирующей этот инвариант.
TETO_PAYTABLE: dict[str, int] = {
    "baguette_crumb": 8,
    "drill_lollipop": 8,
    "utau_note": 10,
    "skull_cringe": 10,
    "teto_chimera": 15,
    "teto_drill_rage": 19,
    "teto_baguette_knight": 23,
    "teto_0401": 40,
    WILD_ID: 40,
}


def win_units(blocks: list[MegaBlock], winning_ids) -> int:
    """Взвешенные «юниты» выигрыша: sum(TETO_PAYTABLE[symbol] * area) по
    блокам с id из `winning_ids`. Деньги шага = bet_per_line * units — точная
    целочисленная арифметика на всех уровнях (шаг/раунд/спин), см. комментарий
    к TETO_PAYTABLE про отказ от дробных множителей."""
    return sum(
        TETO_PAYTABLE[b.symbol_id] * b.area for b in blocks if b.block_id in winning_ids
    )


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


def describe_winning_lines(blocks: list[MegaBlock]) -> list[dict]:
    """Разбор КАЖДОЙ выигравшей payline на геометрию + состав — данные
    ИСКЛЮЧИТЕЛЬНО для анимации (op `evaluate` трейса, см.
    `teto_slot_engine.serialize_animation`), НЕ для денег: выплата считается по
    площади выигравших блоков (`b.area`, см. `_run_tumble_cascade`), а не по
    этому описанию, поэтому расхождение здесь физически не может испортить
    payout — худшее, что оно даст, это криво нарисованная линия.

    Почему это ОТДЕЛЬНАЯ чистая функция-сиблинг, а не изменение
    `evaluate_paylines`. Все три альтернативы разбиваются об уже существующие
    тесты, причём НЕ об "лишний тест, который можно переписать", а об
    осмысленную структурную проверку порядка вызовов:
      1. Третий элемент в возврате (`bool, set, list`) — ломает 8 мест
         распаковки `has_win, winning_ids = evaluate_paylines(...)`
         (`test_teto_tumble.py` x4, `test_teto_drillhunt.py` x3 + шпион в
         `test_teto_slot_engine.py`).
      2. Опциональный out-параметр (`evaluate_paylines(blocks, lines_out=[])`)
         — ломает `test_teto_slot_engine.py::
         test_drill_hunt_never_fires_before_tumble_cascade_exhausted`: он
         monkeypatch'ит `eng.evaluate_paylines` шпионом РОВНО ОДНОГО аргумента
         (`def spy_evaluate(blocks)`). Связывающее ограничение — арность
         ВЫЗОВА в движке, а не подпись самой функции: любой лишний аргумент на
         этом call site — `TypeError` внутри шпиона.
      3. Переключить движок на новое имя (`evaluate_paylines_detailed`) — тот
         же тест патчит именно ИМЯ `eng.evaluate_paylines`; если движок
         перестанет его звать, `call_log` не увидит ни одного evaluate,
         `last_eval_has_win` останется `None` и `assert last_eval_has_win is
         False` упадёт на первом же вызове Дрель-Ханта.
    Цена выбранного варианта — трассируемый спин проходит по линиям ДВАЖДЫ
    (сначала `evaluate_paylines`, потом эта функция). Функция ЧИСТАЯ и без
    `rng` — порядок потребления RNG побайтово не меняется, поэтому
    форсированные/сидированные тесты (`test_full_spin_runs_deterministically_
    with_forced_rng`, end-to-end на сиде 76972) не затрагиваются вовсе.

    ТОНКОСТЬ, на которой ошибается любой наивный оверлей paylines: `length` —
    это число подряд совпавших СТОЛБЦОВ слева (3..6, длина "прогона", который
    подсвечивает анимация), а `block_ids` — РАЗЛИЧНЫЕ id блоков на этих
    столбцах в порядке слева направо, поэтому `len(block_ids) <= length`.
    Расхождение возникает из-за мегаблоков: `evaluate_paylines` кладёт id блока
    в `matched_ids` ОДИН РАЗ НА КАЖДЫЙ совпавший столбец и дедуплицирует только
    потом (через `set`), так что блок шириной 2 занимает на линии ДВА
    позиционных места. Отдавать наружу только дедуплицированный список было бы
    ловушкой: фронт, рисующий полилинию по `len(block_ids)` точкам, не дотянул
    бы её до конца прогона.

    `cells` — `[[row, col], ...]` для столбцов `0..length-1` этой линии, т.е.
    самодостаточная геометрия: фронту НЕ нужна копия `TETO_PAYLINES` — ровно
    поэтому состоявшееся расширение 3 -> 50 линий правок на клиенте не
    потребовало (и смены `TRACE_SCHEMA_VERSION` тоже)."""
    cell_to_block: dict[tuple[int, int], MegaBlock] = {}
    for b in blocks:
        for cell in b.cells:
            cell_to_block[cell] = b

    described: list[dict] = []

    # Тело цикла намеренно повторяет `evaluate_paylines` 1:1 (та же выборка
    # клеток, тот же target, тот же break) — сознательный дубль ~10 строк
    # вместо вынесения общего хелпера: любой общий хелпер пришлось бы вызывать
    # ИЗ `evaluate_paylines`, а её тело — то самое место, которое тест выше
    # патчит шпионом; лишний уровень косвенности там же превратил бы
    # структурную проверку порядка вызовов в проверку деталей реализации.
    for line_index, line in enumerate(TETO_PAYLINES):
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

        if len(matched_ids) < 3:
            continue

        length = len(matched_ids)
        distinct_ids: list[int] = []
        for block_id in matched_ids:
            if block_id not in distinct_ids:
                distinct_ids.append(block_id)

        described.append({
            "line_index": line_index,
            "symbol_id": target,
            "length": length,
            "block_ids": distinct_ids,
            "cells": [[line[col], col] for col in range(length)],
        })

    return described


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
