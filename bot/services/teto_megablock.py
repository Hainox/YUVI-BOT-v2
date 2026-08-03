"""Сетка и мегаблоки слота "Тето Брейнрот: Дрель-Хант" (roadmap.md, концепт 6)
— порт механики Hades Gigablox: сетка 6x6, символы сливаются в прямоугольные
мегаблоки (1x1 одиночки .. 6x6 "весь экран"), а не сидят по одной клетке на
столбец, как в `slot_engine.py` (Azumanga).

Чистые функции, без доступа к БД/`economy_service` — та же дисциплина, что и
`slot_engine.py`. `rng` — ВСЕГДА явный параметр (`bot.services.casino_service._rng`
в проде), никогда module-level seam: этот слот делит фриспин-цикл с обычным
спином внутри одного `play_one_spin` (см. `teto_slot_engine.py`), а не
доигрывает бонус отдельным авто-циклом на module-level `_rng`, как
`slot_engine.evaluate_grid` — там это безопасно (проверено RNG-аудитом отдельно),
здесь тот же паттерн был бы дырой (см. `docs/roadmap.md`, разбор конфликтов
архитектуры Тето).

Партиция сетки — ИНВАРИАНТ: каждая из 36 клеток принадлежит РОВНО одному
`MegaBlock` (включая вырожденные 1x1), `sum(area)==36`, без пропусков и
наложений (`assert_valid_partition`) — держится через ВЕСЬ спин, не только на
первичном заполнении (см. тесты `test_teto_slot_engine.py`)."""

from __future__ import annotations

from dataclasses import dataclass

LOW_SYMBOLS = ["baguette_crumb", "drill_lollipop", "utau_note", "skull_cringe"]
HIGH_SYMBOLS = ["teto_chimera", "teto_drill_rage", "teto_baguette_knight", "teto_0401"]
SCATTER_ID = "baguette_cerberus"
# Динамический Wild — вес 0 на барабанах (НЕ входит в ALL_SYMBOLS): появляется
# только через Дрель-Хант (`teto_drillhunt.py::apply_drill_hunt`), никогда через
# обычное заполнение (`form_blocks`) — см. `test_wild_symbol_never_appears_except_via_drill_hunt`.
WILD_ID = "golden_drill"
ALL_SYMBOLS = LOW_SYMBOLS + HIGH_SYMBOLS + [SCATTER_ID]

GRID_SIZE = 6
ALL_CELLS = {(r, c) for r in range(GRID_SIZE) for c in range(GRID_SIZE)}


@dataclass(frozen=True)
class MegaBlock:
    """Прямоугольный блок из `height x width` одинаковых символов, `row`/`col`
    — координаты верхней левой клетки. `frozen=True` — конверсия в Wild
    (Дрель-Хант) и гравитация (тумбл) не мутируют блок на месте, а строят
    новый через `dataclasses.replace(...)`, сохраняя `block_id`/геометрию."""

    block_id: int
    symbol_id: str
    row: int
    col: int
    height: int
    width: int

    @property
    def cells(self) -> list[tuple[int, int]]:
        return [
            (r, c)
            for r in range(self.row, self.row + self.height)
            for c in range(self.col, self.col + self.width)
        ]

    @property
    def area(self) -> int:
        return self.height * self.width


def assert_valid_partition(blocks: list[MegaBlock]) -> None:
    """Инвариант полной партиции 6x6 (см. докстринг модуля) — вызывается и
    тестами, и самим движком между шагами (`teto_slot_engine.py`)."""
    total_area = sum(b.area for b in blocks)
    assert total_area == GRID_SIZE * GRID_SIZE, (
        f"sum(area)={total_area}, ожидалось {GRID_SIZE * GRID_SIZE}"
    )
    seen: set[tuple[int, int]] = set()
    for b in blocks:
        for cell in b.cells:
            r, c = cell
            assert 0 <= r < GRID_SIZE and 0 <= c < GRID_SIZE, f"клетка вне сетки: {cell}"
            assert cell not in seen, f"клетка {cell} покрыта дважды (block_id={b.block_id})"
            seen.add(cell)
    assert seen == ALL_CELLS, f"не все клетки покрыты, пропущено: {ALL_CELLS - seen}"


# Полный пул форм Gigablox из roadmap.md — h,w от 2 до 6 (25 форм), вес
# ∝ area^-1.5 в оригинале. MVP-подмножество ниже (`SHAPE_POOL`) сознательно
# уже — калибровка полного веса ∝area^-1.5 отложена до Monte-Carlo прогона
# паутейбла (см. roadmap.md), сейчас форма выбирается РАВНОВЕРОЯТНО из
# подмножества, которое уже покрывает "часто/средне/крайне редко" без
# ручной подгонки весов.
FULL_SHAPE_POOL_25 = [(h, w) for h in range(2, 7) for w in range(2, 7)]
assert len(FULL_SHAPE_POOL_25) == 25

SHAPE_POOL = [
    (2, 2), (2, 3), (3, 2), (2, 4), (4, 2),
    (3, 3), (2, 6), (6, 2), (4, 4), (6, 6),
]

# Скаттер-блок ограничен area<=16 (макс. 4x4) — инвариант формулы фриспинов:
# один скаттер-мегаблок никогда не может быть больше 4x4, иначе один спин мог
# бы дать нереалистично много "скаттеров одним блоком" при area^-1.5-весах.
MEGA_BLOCK_SCATTER_AREA_CAP = 16
# ~10% шанс, что за один вызов form_blocks вообще попытается разместить
# мегаблок (area>=4) — остальные пустые клетки всегда одиночные 1x1.
MEGA_BLOCK_ATTEMPT_DENOM = 10

# Веса символов при заполнении (Monte-Carlo калибровка, roadmap.md: «обязателен
# Monte-Carlo прогон паутейбла/весов мегаблоков до продакшена»). До калибровки
# заполнение было РАВНОВЕРОЯТНЫМ по 9 символам — на доске из 36 клеток это
# давало в среднем 4 скаттер-БЛОКА, и порог фриспинов (>=5 блоков,
# `teto_slot_engine.FREESPIN_SCATTER_MIN`) пробивался КАЖДЫМ ~2.5-м спином
# (замерено симуляцией: 0.40 триггера/спин) — бонус переставал быть событием,
# а 77% всего RTP уезжало во фриспины. Формула фриспинов зафиксирована
# владельцем и не трогается; калибровочная ручка — именно ЧАСТОТА скаттера в
# заполнении, как и в оригинальном Gigablox, где скаттер на барабанах редок.
# Вес 1/27 на клетку -> в среднем ~1.3 скаттер-блока на доску -> триггер
# ~1 к 100 спинов (перемерено скриптом scripts/calibrate_teto_rtp.py).
#
# Веса выражены ПУЛОМ С ПОВТОРАМИ (список, где символ встречается weight раз),
# а не rng.choices(weights=...) — намеренно: единственный RNG-контракт движка
# это .choice(seq)/.randint(a,b) (см. _ForcedRng-стабы в тестах), и пул
# сохраняет его. Порядок пула держит LOW_SYMBOLS[0] ПЕРВЫМ элементом, чтобы
# детерминированные стабы (choice -> seq[0]) продолжали получать тот же символ,
# что и при старом равновероятном списке.
WEIGHTS: dict[str, int] = {
    "baguette_crumb": 4,
    "drill_lollipop": 4,
    "utau_note": 4,
    "skull_cringe": 4,
    "teto_chimera": 3,
    "teto_drill_rage": 3,
    "teto_baguette_knight": 2,
    "teto_0401": 2,
    SCATTER_ID: 1,
}
assert set(WEIGHTS) == set(ALL_SYMBOLS), "WEIGHTS должен покрывать ровно ALL_SYMBOLS"

# Пул одиночного заполнения: символ повторён weight раз (27 элементов).
FILL_POOL: list[str] = [s for s in ALL_SYMBOLS for _ in range(WEIGHTS[s])]

# Пул символа мегаблока: те же веса, но скаттер вынесен отдельно — его
# допустимость зависит от площади формы (MEGA_BLOCK_SCATTER_AREA_CAP).
MEGA_POOL_NO_SCATTER: list[str] = [
    s for s in (LOW_SYMBOLS + HIGH_SYMBOLS) for _ in range(WEIGHTS[s])
]
MEGA_POOL_WITH_SCATTER: list[str] = MEGA_POOL_NO_SCATTER + [SCATTER_ID] * WEIGHTS[SCATTER_ID]


def form_blocks(rng, empty_cells: set[tuple[int, int]], *, start_id: int = 0) -> list[MegaBlock]:
    """Заполняет ПЕРЕДАННЫЕ пустые клетки блоками — MVP: не более ОДНОГО
    настоящего мегаблока (area>=4) за вызов; остальные клетки — одиночные 1x1
    из обычного заполнения. С шансом `rng.randint(1, MEGA_BLOCK_ATTEMPT_DENOM)
    == 1` пытаемся разместить один мегаблок; иначе (или если ни одна позиция
    не вписывается в `empty_cells`) — fallback на одиночные клетки.

    `start_id` — следующий свободный `block_id` (вызывающий отвечает за
    отсутствие коллизий между вызовами в рамках одного спина, см.
    `teto_tumble.py::resolve_tumble`)."""
    cells = set(empty_cells)
    if not cells:
        return []

    blocks: list[MegaBlock] = []
    next_id = start_id
    mega_cells: set[tuple[int, int]] = set()

    if rng.randint(1, MEGA_BLOCK_ATTEMPT_DENOM) == 1:
        h, w = rng.choice(SHAPE_POOL)
        valid_positions = []
        for r0 in range(0, GRID_SIZE - h + 1):
            for c0 in range(0, GRID_SIZE - w + 1):
                rect = {(r, c) for r in range(r0, r0 + h) for c in range(c0, c0 + w)}
                if rect <= cells:
                    valid_positions.append((r0, c0))

        if valid_positions:
            idx = rng.randint(0, len(valid_positions) - 1)
            top, left = valid_positions[idx]

            area = h * w
            # Взвешенный пул вместо равновероятного списка — см. WEIGHTS выше.
            if area <= MEGA_BLOCK_SCATTER_AREA_CAP:
                symbol_candidates = MEGA_POOL_WITH_SCATTER
            else:
                symbol_candidates = MEGA_POOL_NO_SCATTER
            mega_symbol = rng.choice(symbol_candidates)

            mega_cells = {(r, c) for r in range(top, top + h) for c in range(left, left + w)}
            blocks.append(MegaBlock(
                block_id=next_id, symbol_id=mega_symbol,
                row=top, col=left, height=h, width=w,
            ))
            next_id += 1
        # иначе позиция не найдена — fallback: нет мегаблока, все клетки одиночные

    for (r, c) in sorted(cells - mega_cells):
        symbol = rng.choice(FILL_POOL)
        blocks.append(MegaBlock(block_id=next_id, symbol_id=symbol, row=r, col=c, height=1, width=1))
        next_id += 1

    return blocks
