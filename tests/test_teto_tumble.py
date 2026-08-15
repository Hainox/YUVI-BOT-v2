"""Тесты payline-оценки и тумбл-каскада слота "Тето Брейнрот"
(`bot/services/teto_tumble.py`). Plain pytest, чистые функции.

Доказывают:
- `evaluate_paylines`: leftmost non-wild как target, wild подставляется вместо
  любого символа кроме scatter, минимум 3 подряд слева направо, scatter как
  target пропускается (платит суммой площадей скаттер-блоков через
  `count_scatter_blocks`, не по линии).
- `resolve_tumble`: выигравший блок убирается ЦЕЛИКОМ; многоклеточный блок
  падает RIGID BODY (единым целым — не разрывается на части, даже если под
  разными его столбцами разная "высота падения"); партиция держится после
  гравитации + добора.
- `count_scatter_blocks` считает СУММУ ПЛОЩАДЕЙ (клеток) скаттер-блоков, а не
  их число (РЕШЕНИЕ ОТ 2026-08-06, отменяет прежнее "считаем блоки, не
  клетки" от 2026-07-30 — см. `bot/services/teto_tumble.py`)."""

from __future__ import annotations

from bot.services.teto_megablock import ALL_CELLS
from bot.services.teto_megablock import ALL_SYMBOLS
from bot.services.teto_megablock import GRID_SIZE
from bot.services.teto_megablock import HIGH_SYMBOLS
from bot.services.teto_megablock import LOW_SYMBOLS
from bot.services.teto_megablock import SCATTER_ID
from bot.services.teto_megablock import WILD_ID
from bot.services.teto_megablock import MegaBlock
from bot.services.teto_megablock import assert_valid_partition
from bot.services.teto_tumble import TETO_PAYLINES
from bot.services.teto_tumble import TETO_PAYTABLE
from bot.services.teto_tumble import count_scatter_blocks
from bot.services.teto_tumble import evaluate_paylines
from bot.services.teto_tumble import resolve_tumble


class _ForcedRng:
    """См. `tests/test_teto_megablock.py::_ForcedRng` — тот же стаб,
    продублирован здесь по конвенции файла (аналог `_ForcedGridRng` в
    `tests/test_slot_engine.py`, локальный на каждый тестовый модуль)."""

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


# Три РАЗНЫХ не-wild/не-scatter символа «тихой» полосной базы — см. _build_board.
_STRIPE_SYMBOLS = ("baguette_crumb", "drill_lollipop", "utau_note")


def _build_board(overrides: dict) -> list[MegaBlock]:
    """Полная доска 36 одиночных 1x1 блоков: базовый символ клетки зависит
    ТОЛЬКО от столбца — `_STRIPE_SYMBOLS[col % 3]` («столбцовые полосы с
    периодом 3»), `overrides` поверх врезает проверяемую фигуру.

    Почему полосы — универсальная «тихая» база под ЛЮБОЙ набор линий: каждая
    payline продвигается ровно на один столбец за позицию, поэтому читает
    S0,S1,S2,S0,S1,S2 из трёх РАЗНЫХ символов — три подряд равных не
    собираются ни на одной линии никакой формы, а leftmost-тройка всегда
    различна (прогон обрывается на второй позиции). Прежний подход
    «однородный fill + явно задать клетки каждой линии» работал на
    3-линейном MVP и НЕ масштабируется на полные 50 (`TETO_PAYLINES`): теперь
    КАЖДАЯ клетка доски лежит хотя бы на горизонтали своей строки, и
    однородная база выигрывает сама по себе на непроверяемой линии (этот
    класс бага уже ловился на прототипе, см. roadmap.md/отчёт по Тето).

    Символ врезки ОБЯЗАН отличаться от всех трёх полосных — иначе врезка
    может продлить прогон линии, ныряющей через её клетки; линейных символов
    8 (LOW + HIGH), полосы занимают 3, так что места достаточно. Точность
    каждой врезки (выигрыш ровно там, где задуман) проверяется самим
    `evaluate_paylines` в каждом тесте, а не постулируется."""
    blocks = []
    bid = 0
    for r in range(GRID_SIZE):
        for c in range(GRID_SIZE):
            sym = overrides.get((r, c), _STRIPE_SYMBOLS[c % 3])
            blocks.append(MegaBlock(bid, sym, r, c, 1, 1))
            bid += 1
    return blocks


# ---------------------------------------------------------------------------
# evaluate_paylines
# ---------------------------------------------------------------------------


def test_evaluate_paylines_detects_win_on_middle_line():
    """Средняя горизонталь — `TETO_PAYLINES[2] == [2,2,2,2,2,2]` (в полном
    наборе 50 линий шесть горизонталей идут первыми, по строкам 0..5; на
    3-линейном MVP она была индексом 0). Врезка teto_chimera в (2,0)..(2,3)
    даёт прогон РОВНО 4 на ней и ни на какой другой: (2,4) — полосный
    drill_lollipop — обрывает прогон, а остальные 49 линий глушит полосная
    база (см. `_build_board`)."""
    assert TETO_PAYLINES[2] == [2, 2, 2, 2, 2, 2], "тест целится в среднюю горизонталь"
    blocks = _build_board({(2, c): "teto_chimera" for c in range(4)})
    has_win, winning_ids = evaluate_paylines(blocks)
    assert has_win is True
    by_cell = {b.cells[0]: b.block_id for b in blocks}
    assert winning_ids == {by_cell[(2, c)] for c in range(4)}


def test_evaluate_paylines_no_win_when_run_too_short():
    """Прогон из 2 (минимум для выплаты — 3) не платит: врезка из двух
    teto_chimera в (2,0),(2,1). Все линии, начинающиеся этой парой
    ([2,2,...]-семейство: горизонталь, лесенки, бугорок), на третьей позиции
    читают полосный символ и обрываются на длине 2; остальное — чистая
    полосная база без единого прогона."""
    blocks = _build_board({(2, 0): "teto_chimera", (2, 1): "teto_chimera"})
    has_win, winning_ids = evaluate_paylines(blocks)
    assert has_win is False
    assert winning_ids == set()


def test_evaluate_paylines_wild_substitutes_for_any_symbol_except_scatter():
    """WILD на (2,0) + прогон teto_chimera в (2,1)..(2,3): target — leftmost
    НЕ-wild символ (teto_chimera), wild засчитывается за него — прогон 4 на
    средней горизонтали. Wild пробивает полосную защиту только в своём
    столбце: любая другая линия, начинающаяся с (2,0), после wild читает два
    РАЗНЫХ полосных/врезанных символа и обрывается на длине 2."""
    blocks = _build_board({
        (2, 0): WILD_ID,
        (2, 1): "teto_chimera", (2, 2): "teto_chimera", (2, 3): "teto_chimera",
    })
    has_win, winning_ids = evaluate_paylines(blocks)
    assert has_win is True
    by_cell = {b.cells[0]: b.block_id for b in blocks}
    assert winning_ids == {by_cell[(2, c)] for c in range(4)}


def test_evaluate_paylines_scatter_as_leftmost_symbol_never_pays_by_line():
    # Скаттер как САМЫЙ ЛЕВЫЙ (target-определяющий) символ на линии
    # пропускается целиком — платит по сумме площадей скаттер-блоков на экране
    # (count_scatter_blocks), не по payline. Вся строка 2 из скаттеров:
    # линии, начинающиеся с (2,0), скипаются по target == SCATTER_ID, а
    # линии, ныряющие в строку 2 позже, ломаются о скаттер-клетку внутри
    # прогона (wild за скаттер не считается, обычный символ — тем более).
    blocks = _build_board({(2, c): SCATTER_ID for c in range(GRID_SIZE)})
    has_win, winning_ids = evaluate_paylines(blocks)
    assert has_win is False
    assert winning_ids == set()


def test_teto_paylines_full_set_is_valid():
    """Структурный пин полного набора линий (roadmap.md: перенос геометрии
    оригинала 1:1 — РОВНО 50, как в Hades Gigablox). Каждая линия — по одной
    строке на каждый из 6 столбцов в границах сетки: линия неполной длины
    уронила бы `evaluate_paylines` на чтении `line[col]`, строка вне 0..5 —
    на несуществующей клетке. Дубликат линии — тихая двойная оплата одной и
    той же геометрии, поэтому уникальность здесь не эстетика, а деньги."""
    assert len(TETO_PAYLINES) == 50
    for line in TETO_PAYLINES:
        assert len(line) == GRID_SIZE
        assert all(0 <= row < GRID_SIZE for row in line)
    assert len({tuple(line) for line in TETO_PAYLINES}) == len(TETO_PAYLINES), (
        "дубликат payline платил бы одну геометрию дважды"
    )


def test_teto_paytable_covers_line_symbols_and_orders_tiers():
    """Пин состава и тир-порядка `TETO_PAYTABLE`. Ключи — РОВНО линейные
    символы: LOW + HIGH + WILD. Скаттера в таблице НЕТ сознательно: скаттер
    структурно не может войти в линейный выигрыш (target == SCATTER_ID
    скипается, wild за скаттер не считается — см. `evaluate_paylines`),
    поэтому запись для него была бы мёртвой строкой, маскирующей этот
    инвариант. Порядок тиров: любой LOW строго дешевле любого HIGH (иначе
    деление на 4 low / 4 high существовало бы только на бумаге — ровно то,
    что паутейбл и чинил), топ среди не-wild — teto_0401, wild платит как
    топ-символ (он приходит ТОЛЬКО через Дрель-Хант, и его цена — часть
    механики момента). Все значения — положительные int: дробные множители
    сломали бы точный инвариант «сумма evaluate.payout по раунду ==
    round_end.raw_round_payout» (см. комментарий к TETO_PAYTABLE)."""
    assert set(TETO_PAYTABLE) == set(LOW_SYMBOLS + HIGH_SYMBOLS + [WILD_ID])
    assert all(type(v) is int and v > 0 for v in TETO_PAYTABLE.values())
    assert max(TETO_PAYTABLE[s] for s in LOW_SYMBOLS) < min(
        TETO_PAYTABLE[s] for s in HIGH_SYMBOLS
    ), "каждый LOW обязан быть строго дешевле каждого HIGH"
    assert TETO_PAYTABLE["teto_0401"] == max(
        TETO_PAYTABLE[s] for s in LOW_SYMBOLS + HIGH_SYMBOLS
    ), "топ среди не-wild символов — teto_0401"
    assert TETO_PAYTABLE[WILD_ID] == TETO_PAYTABLE["teto_0401"], (
        "wild платит как топ-символ — ценность момента Дрель-Ханта"
    )


def test_count_scatter_blocks_counts_by_area():
    """РЕШЕНИЕ ОТ 2026-08-06 (отменяет прежнее "считаем блоки, не клетки" от
    2026-07-30): один крупный скаттер-мегаблок (растянутый на много клеток)
    считается по СУММЕ СВОИХ КЛЕТОК, не за один скаттер. Блок здесь 4x4 ->
    area=16, поэтому count_scatter_blocks обязан вернуть 16, а не 1."""
    big_scatter = MegaBlock(block_id=0, symbol_id=SCATTER_ID, row=0, col=0, height=4, width=4)  # area=16
    filler_ids = list(range(1, 21))
    other_cells = sorted(ALL_CELLS - set(big_scatter.cells))
    fillers = [MegaBlock(bid, "utau_note", r, c, 1, 1) for bid, (r, c) in zip(filler_ids, other_cells)]
    assert count_scatter_blocks([big_scatter] + fillers) == 16


def test_count_scatter_blocks_sums_areas_of_multiple_blocks_on_board():
    """Прямой пин семантики "сумма одинарных блоков", которую владелец
    специально запросил при развороте 2026-08-06: ДВА скаттер-блока на одной
    доске (1x1 в углу + 2x2 в другом углу) обязаны дать СУММУ их площадей
    (1 + 4 = 5), не число блоков (2) и не площадь только одного из них."""
    small_scatter = MegaBlock(block_id=0, symbol_id=SCATTER_ID, row=0, col=0, height=1, width=1)  # area=1
    big_scatter = MegaBlock(block_id=1, symbol_id=SCATTER_ID, row=4, col=4, height=2, width=2)  # area=4
    covered = set(small_scatter.cells) | set(big_scatter.cells)
    filler_ids = list(range(2, 2 + len(ALL_CELLS) - len(covered)))
    other_cells = sorted(ALL_CELLS - covered)
    fillers = [MegaBlock(bid, "utau_note", r, c, 1, 1) for bid, (r, c) in zip(filler_ids, other_cells)]
    assert count_scatter_blocks([small_scatter, big_scatter] + fillers) == 5


# ---------------------------------------------------------------------------
# resolve_tumble
# ---------------------------------------------------------------------------


def test_resolve_tumble_multi_column_block_falls_as_one_rigid_unit_not_torn_apart():
    """Блок A: 1x3 горизонтальный на row=2, cols 0-2. Блок B: 1x1 на row=5,
    col=1 (прямо под средним столбцом A). Наивная per-column гравитация
    уронила бы col0/col2 до row=5, пока col1 блокирован B — разорвав A на
    части. Rigid-body гравитация обязана сдвинуть A вниз ровно настолько,
    насколько позволяют ВСЕ 3 столбца сразу (останавливается на row=4, на
    строку выше B), сохранив A целым 1x3 блоком."""
    a = MegaBlock(block_id=0, symbol_id="teto_chimera", row=2, col=0, height=1, width=3)
    b = MegaBlock(block_id=1, symbol_id="drill_lollipop", row=5, col=1, height=1, width=1)

    rng = _ForcedRng(choices=ALL_SYMBOLS, randints=[9])  # без попытки мегаблока на доборе
    result = resolve_tumble(rng, [a, b], winning_block_ids=set())

    by_id = {blk.block_id: blk for blk in result}
    assert 0 in by_id and 1 in by_id

    settled_a = by_id[0]
    settled_b = by_id[1]
    assert (settled_a.height, settled_a.width) == (1, 3), "блок A не должен развалиться на части"
    assert settled_a.symbol_id == "teto_chimera"
    assert settled_a.row == 4, f"ожидалась остановка rigid-body на row=4, получено row={settled_a.row}"
    assert (settled_b.row, settled_b.col) == (5, 1)  # B уже стоял на полу, не двигается


def test_resolve_tumble_2x2_block_falls_as_rigid_body_exact_landing_row():
    """Мегаблок 2x2 (ДВУМЕРНЫЙ — height>1 И width>1, не только широкий-но-плоский,
    как в тесте выше) в левом верхнем углу; под ним (row=2, col=0..1) убираем
    выигравшие одиночные блоки -> мегаблок обязан упасть КАК ЕДИНОЕ ЦЕЛОЕ ровно
    на одну строку вниз (в rows 1-2), а не развалиться на части."""
    mega = MegaBlock(block_id=0, symbol_id="teto_chimera", row=0, col=0, height=2, width=2)
    covered = set(mega.cells)  # {(0,0),(0,1),(1,0),(1,1)}

    singles = []
    id_by_cell = {}
    next_id = 1
    for (r, c) in sorted(ALL_CELLS - covered):
        singles.append(MegaBlock(block_id=next_id, symbol_id="utau_note", row=r, col=c, height=1, width=1))
        id_by_cell[(r, c)] = next_id
        next_id += 1

    blocks = [mega] + singles
    assert_valid_partition(blocks)  # sanity перед тестом

    winning_ids = {id_by_cell[(2, 0)], id_by_cell[(2, 1)]}

    rng = _ForcedRng(choices=ALL_SYMBOLS, randints=[7])  # без мегаблока на доборе
    result = resolve_tumble(rng, blocks, winning_ids)

    assert_valid_partition(result)

    moved_mega = next(b for b in result if b.block_id == 0)
    assert (moved_mega.height, moved_mega.width) == (2, 2), "2x2 блок не должен развалиться на части"
    assert moved_mega.symbol_id == "teto_chimera"
    assert (moved_mega.row, moved_mega.col) == (1, 0), "упал ровно на 1 строку (столбцы под ним были заняты ниже row=3)"


def test_resolve_tumble_removes_winning_block_entirely_even_if_multi_cell():
    """2x2 мегаблок, где выигравшими считаются ВСЕ его клетки (весь block_id)
    — обязан быть убран ЦЕЛИКОМ, не частично."""
    mega = MegaBlock(block_id=0, symbol_id="teto_0401", row=0, col=0, height=2, width=2)
    filler_ids = list(range(1, 33))
    other_cells = sorted(ALL_CELLS - set(mega.cells))
    fillers = [MegaBlock(bid, "utau_note", r, c, 1, 1) for bid, (r, c) in zip(filler_ids, other_cells)]

    rng = _ForcedRng(choices=ALL_SYMBOLS, randints=[9])
    result = resolve_tumble(rng, [mega] + fillers, winning_block_ids={0})

    assert 0 not in {b.block_id for b in result}


def test_resolve_tumble_result_respects_partition_invariant():
    mega = MegaBlock(block_id=0, symbol_id="teto_0401", row=0, col=0, height=2, width=2)
    filler_ids = list(range(1, 33))
    other_cells = sorted(ALL_CELLS - set(mega.cells))
    fillers = [MegaBlock(bid, "utau_note", r, c, 1, 1) for bid, (r, c) in zip(filler_ids, other_cells)]

    rng = _ForcedRng(choices=ALL_SYMBOLS, randints=[9])
    result = resolve_tumble(rng, [mega] + fillers, winning_block_ids={0})
    assert_valid_partition(result)


def test_resolve_tumble_new_block_ids_never_collide_with_removed_winning_ids():
    """`resolve_tumble` считает следующий свободный `block_id` от максимума
    среди ВСЕХ переданных блоков (включая уже удалённые выигравшие), а не
    только среди выживших — иначе новый блок на освободившейся клетке мог бы
    получить тот же id, что и только что удалённый выигравший блок этого же
    шага (найдено и отсеяно на прототипе, см. roadmap.md/отчёт по Тето).

    Доска — 36 одиночных 1x1 блоков id=0..35 (row-major). Убираем block_id=35
    (row=5,col=5): гравитация сдвигает весь столбец col=5 вниз на одну
    клетку (выжившие блоки (0,5)..(4,5) оседают на (1,5)..(5,5)), поэтому
    РОВНО ОДИН генуинно НОВЫЙ блок от `form_blocks` появляется на (0,5) —
    верхушке освободившегося столбца, а не на месте самого удалённого блока."""
    blocks = [
        MegaBlock(block_id=i, symbol_id="utau_note", row=r, col=c, height=1, width=1)
        for i, (r, c) in enumerate(sorted(ALL_CELLS))
    ]
    winning_ids = {35}  # последний по порядку id (row=5, col=5)

    rng = _ForcedRng(choices=ALL_SYMBOLS, randints=[9])
    result = resolve_tumble(rng, blocks, winning_ids)

    all_ids = [b.block_id for b in result]
    assert len(all_ids) == len(set(all_ids)), "block_id обязаны быть уникальны после тумбла"

    new_blocks = [b for b in result if b.block_id not in range(35)]
    assert len(new_blocks) == 1, "ожидался ровно один генуинно новый блок от form_blocks"
    assert new_blocks[0].block_id == 36, "новый id обязан продолжаться ПОСЛЕ максимума среди ВСЕХ исходных блоков"
    assert (new_blocks[0].row, new_blocks[0].col) == (0, 5)
