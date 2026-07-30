"""Дрель-Хант и лестница множителя слота "Тето Брейнрот" — порт механики Wild
Hunt (Hades Gigablox): Wild не падает на барабанах, а внедряется постфактум
ПОСЛЕ того, как обычный тумбл-каскад исчерпан.

Из трёх прототипированных вариантов Дрель-Ханта выбран **Вариант C**
("конвертация целого блока в Wild" + опциональная ОДНА дополнительная волна
тумбла, если инъекция породила новый выигрыш, засчитываемая в общий
`TUMBLE_HARD_CAP`) — единственный вариант, эмпирически не нарушивший
буквальную гарантию roadmap.md "во фриспинах Дрель-Хант срабатывает
гарантированно >=1 раз за спин" на стресс-тесте прототипа (20 000 случайных
досок): у альтернативного варианта "вырезать новый прямоугольник в свободном
месте" гарантия реально ломалась в 18/20000 (0.09%) случаев, когда один
мегаблок 6x6 занимал всю сетку целиком. roadmap.md фиксирует только само
требование (гарантия >=1 во фриспинах), не сравнение вариантов — сам
прототип и его стресс-тесты жили в отдельной сессии проектирования, не в
этом репозитории; этот докстринг — единственная запись обоснования выбора,
дошедшая до кода.

Лестница множителя (фича ТОЛЬКО фриспинов — **Вариант Y**, "новый тир
действует со СЛЕДУЮЩЕГО раунда"): счёт = число клеток, сконвертированных
Дрель-Хантом за фриспины; `LadderState` создаётся заново при входе во
фриспин-режим и обновляется РОВНО РАЗ за фриспин-раунд — ЗАФИКСИРОВАНО
владельцем бота 2026-07-30: очки Дрель-Ханта в базовой (не фриспин) игре в
лестницу не идут, `teto_slot_engine.py` создаёт `LadderState()` только на
вход во фриспины. Выбран над альтернативой "новый тир задним числом в этом же
раунде" — та эмпирически не сломана, но несёт задокументированный риск
двойного умножения при неаккуратной передаче `raw_round_payout` между
раундами фриспин-цикла и ретроактивно переписывает уже показанный игроку
выигрыш (см. roadmap.md/отчёт по Тето)."""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import replace
from typing import NamedTuple

from bot.services.teto_megablock import SCATTER_ID
from bot.services.teto_megablock import WILD_ID
from bot.services.teto_megablock import MegaBlock
from bot.services.teto_tumble import describe_winning_lines
from bot.services.teto_tumble import evaluate_paylines
from bot.services.teto_tumble import resolve_tumble

# ~10% шанс срабатывания Дрель-Ханта в БАЗОВОЙ игре (не гарантирован) — во
# фриспинах `guaranteed=True` игнорирует этот шанс целиком.
DRILL_HUNT_BASE_TRIGGER_DENOM = 10


class DrillHuntOutcome(NamedTuple):
    """`cells_converted` — площадь блока, конвертированного в Wild (0, если
    Дрель-Хант не сработал). `triggered_new_tumble` — сработала ли РОВНО ОДНА
    дополнительная волна тумбла из-за нового выигрыша, порождённого инъекцией.
    `wave_winning_block_ids`/`wave_cells_won` — id и суммарная площадь блоков,
    вошедших в выигрыш ИМЕННО этой волны (до их удаления гравитацией) —
    отдельно атрибутируемый payout волны.

    --- Три хвостовых поля НИЖЕ существуют только ради анимации ---

    `wild_block_id` — id блока, ставшего Wild (`None`, если Дрель-Хант не
    сработал). `blocks_after_injection` — доска СРАЗУ ПОСЛЕ инъекции Wild и ДО
    волны. `wave_winning_lines` — `describe_winning_lines` на этой же доске,
    т.е. геометрия линий, которые выиграла ИМЕННО волна.

    Почему эта информация едет НА ВЫХОДЕ, а не через новые параметры
    `apply_drill_hunt`: два существующих теста подменяют её шпионами РОВНО
    той же арности — `test_teto_slot_engine.py::spy_drill` и
    `test_base_round_drill_hunt_cells_never_feed_the_ladder_score::
    spy_apply_drill_hunt`, оба `(rng, blocks, *, guaranteed,
    tumble_hard_cap_remaining)`. Любой лишний аргумент (в т.ч. `trace=`) на
    call site движка — `TypeError` внутри шпиона; подпись `apply_drill_hunt`
    поэтому НЕПРИКОСНОВЕННА, а вся новая информация уезжает в возврате.

    Почему поля ДОПИСАНЫ в хвост с дефолтами: ни один тест не конструирует
    `DrillHuntOutcome` вручную, все 5 мест конструирования — внутри этого
    модуля (и все 5 заполняют поля реальными значениями), чтение по имени
    атрибута не затрагивается, а `outcome._replace(cells_converted=30)` в
    `test_base_round_drill_hunt_cells_never_feed_the_ladder_score` продолжает
    работать поверх дописанных полей. Дефолты — страховка на случай
    гипотетической позиционной конструкции извне, полагаться на них НЕ следует:
    `blocks_after_injection` фактически заполняется ВСЕГДА (равен `blocks`,
    когда волна не сработала; равен копии входной доски, когда Дрель-Хант вообще
    не сработал).

    Конкретный баг, который эти поля закрывают: старый трейс отдавал в op
    `drill_hunt` доску ПОСЛЕ волны, поэтому кадр "дрель приземлилась на блок X,
    он стал золотой дрелью" был из трейса невосстановим В ПРИНЦИПЕ — фронт не
    мог ни показать посадку дрели, ни подсветить то, что волна потом снесла
    (блоки уже отсутствовали на единственном доступном кадре)."""

    blocks: list[MegaBlock]
    cells_converted: int
    triggered_new_tumble: bool
    wave_winning_block_ids: frozenset[int]
    wave_cells_won: int
    # --- добавлено для анимации, только хвостом и с дефолтами (см. докстринг) ---
    wild_block_id: int | None = None
    blocks_after_injection: list[MegaBlock] | None = None
    wave_winning_lines: tuple[dict, ...] = ()


def apply_drill_hunt(
    rng, blocks: list[MegaBlock], *, guaranteed: bool, tumble_hard_cap_remaining: int
) -> DrillHuntOutcome:
    """Триггер: `guaranteed=True` — всегда; `guaranteed=False` —
    `rng.randint(1, 10) == 1`. Если сработало: `eligible` = блоки, чей
    `symbol_id` НЕ в (`WILD_ID`, `SCATTER_ID`); `picked` выбирается через
    `rng.choice` НАД СПИСКОМ `block_id` (не объектов MegaBlock) — детерминизм
    выбора не зависит от порядка списка блоков. Выбранный блок заменяется на
    `dataclasses.replace(picked, symbol_id=WILD_ID)` — геометрия и `block_id`
    сохраняются.

    После инъекции борд переоценивается через `evaluate_paylines`. Если
    появился НОВЫЙ выигрыш и `tumble_hard_cap_remaining > 0` — он
    обрабатывается как РОВНО ОДИН дополнительный шаг обычного тумбл-цикла
    (`resolve_tumble`). Дрель-Хант НЕ запускается повторно внутри этого же
    вызова (терминальность рекурсии) — если получившийся после волны борд
    снова содержит выигрыш, это ответственность ВНЕШНЕГО тумбл-цикла
    (`teto_slot_engine.run_tumble_cascade`), не этой функции. Если
    `tumble_hard_cap_remaining <= 0` — волна не производится (Wild остаётся
    на доске "непроверенным") — осознанное усечение, аналогичное
    `FREESPINS_HARD_CAP`.

    ПОДПИСЬ НЕПРИКОСНОВЕННА (её патчат шпионы точной арности в двух тестах —
    см. докстринг `DrillHuntOutcome`), поэтому кадры/id, нужные анимации,
    возвращаются в хвостовых полях `DrillHuntOutcome`
    (`wild_block_id`/`blocks_after_injection`/`wave_winning_lines`), а не
    записываются функцией куда-то самостоятельно: этот модуль вообще не знает
    ни про трейс, ни про сериализацию — их собирает исключительно
    `teto_slot_engine`."""
    if guaranteed:
        triggered = True
    else:
        triggered = rng.randint(1, DRILL_HUNT_BASE_TRIGGER_DENOM) == 1

    if not triggered:
        return DrillHuntOutcome(
            list(blocks), 0, False, frozenset(), 0,
            wild_block_id=None, blocks_after_injection=list(blocks), wave_winning_lines=(),
        )

    eligible = [b for b in blocks if b.symbol_id not in (WILD_ID, SCATTER_ID)]
    if not eligible:
        # Структурно недостижимо на нормальной доске (WILD никогда не в пуле
        # заполнения; полностью-scatter/wild борд не встретился ни разу на
        # 20000+ случайных партиций на прототипе), но держим defensive no-op.
        return DrillHuntOutcome(
            list(blocks), 0, False, frozenset(), 0,
            wild_block_id=None, blocks_after_injection=list(blocks), wave_winning_lines=(),
        )

    eligible_ids = [b.block_id for b in eligible]
    picked_id = rng.choice(eligible_ids)
    picked = next(b for b in eligible if b.block_id == picked_id)

    new_blocks: list[MegaBlock] = []
    cells_converted = 0
    for b in blocks:
        if b.block_id == picked.block_id:
            new_blocks.append(replace(b, symbol_id=WILD_ID))
            cells_converted = b.area
        else:
            new_blocks.append(b)

    has_win, winning_ids = evaluate_paylines(new_blocks)
    if not has_win:
        return DrillHuntOutcome(
            new_blocks, cells_converted, False, frozenset(), 0,
            wild_block_id=picked.block_id, blocks_after_injection=new_blocks, wave_winning_lines=(),
        )

    if tumble_hard_cap_remaining <= 0:
        return DrillHuntOutcome(
            new_blocks, cells_converted, False, frozenset(), 0,
            wild_block_id=picked.block_id, blocks_after_injection=new_blocks, wave_winning_lines=(),
        )

    wave_cells_won = sum(b.area for b in new_blocks if b.block_id in winning_ids)
    # `describe_winning_lines` считается ТОЛЬКО в этой ветке (волна реально
    # состоялась) — не безусловно: на путях выше её результат был бы либо
    # пустым, либо описывал бы линии, которые никто не убирал, а лишний проход
    # по paylines на каждом вызове Дрель-Ханта не нужен ни деньгам, ни фронту.
    wave_winning_lines = tuple(describe_winning_lines(new_blocks))
    final_blocks = resolve_tumble(rng, new_blocks, winning_ids)
    return DrillHuntOutcome(
        final_blocks, cells_converted, True, frozenset(winning_ids), wave_cells_won,
        wild_block_id=picked.block_id,
        blocks_after_injection=new_blocks,
        wave_winning_lines=wave_winning_lines,
    )


# Пороги лестницы (score -> multiplier), перенесены 1:1 из roadmap.md — НЕ
# масштабируются, потому что макс. блок = 36 клеток (весь экран), как и в
# оригинале Hades Gigablox. +2 фриспина за каждый ВПЕРВЫЕ пересечённый порог
# (в т.ч. если один Дрель-Хант перепрыгивает сразу несколько порогов).
LADDER: list[tuple[int, int]] = [(10, 2), (15, 3), (20, 5), (25, 10)]
MAX_BOARD_SCORE = 36  # 6x6, весь экран Дрель-Хантом


@dataclass
class LadderState:
    """`multiplier` — активный множитель НА ВХОДЕ в текущий фриспин-раунд
    (Вариант Y: пересечение порога В этом раунде начинает действовать только
    со следующего). Создаётся заново (`LadderState()`) при входе во
    фриспин-режим — см. докстринг модуля про ограничение "только фриспины"."""

    score: int = 0
    multiplier: int = 1
    crossed_thresholds: set = None

    def __post_init__(self):
        if self.crossed_thresholds is None:
            self.crossed_thresholds = set()


def apply_ladder(
    ladder: LadderState, cells_converted: int, raw_round_payout: int
) -> tuple[LadderState, int, int]:
    """Вариант Y: `final_round_payout = raw_round_payout * СТАРЫЙ` (активный
    на входе) `multiplier`. `score`/`multiplier`/`crossed_thresholds`
    обновляются строго ПОСЛЕ расчёта payout текущего раунда — пересечение
    порога в этом раунде начинает действовать только со следующего, никогда
    не ретроактивно. Возвращает `(новый LadderState, extra_freespins_awarded,
    final_round_payout)`."""
    assert cells_converted >= 0, "score строго монотонен — Дрель-Хант только конвертирует клетки"

    old_multiplier = ladder.multiplier
    final_round_payout = raw_round_payout * old_multiplier

    new_score = min(ladder.score + cells_converted, MAX_BOARD_SCORE)
    new_crossed = set(ladder.crossed_thresholds)
    new_multiplier = old_multiplier
    extra_freespins = 0

    for threshold, mult in LADDER:
        if new_score >= threshold and threshold not in new_crossed:
            new_crossed.add(threshold)
            extra_freespins += 2
            new_multiplier = mult

    new_state = LadderState(score=new_score, multiplier=new_multiplier, crossed_thresholds=new_crossed)
    return new_state, extra_freespins, final_round_payout
