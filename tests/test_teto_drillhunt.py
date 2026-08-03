"""Тесты Дрель-Ханта (Вариант C) и лестницы множителя (Вариант Y)
(`bot/services/teto_drillhunt.py`). Plain pytest, чистые функции.

Доказывают (см. докстринг модуля для обоснования выбора вариантов):
- `apply_drill_hunt`: конвертация блока в Wild терминальна (НЕ рекурсивна) —
  даже если результат после доп. волны тумбла снова выглядит выигрышным,
  Дрель-Хант не перезапускается сам; `resolve_tumble` вызывается РОВНО ОДИН
  раз, когда инъекция создаёт новый выигрыш и остался бюджет хардкапа.
- `tumble_hard_cap_remaining == 0` подавляет доп. волну, но НЕ саму конверсию
  (Wild физически остаётся на доске "непроверенным" — осознанное усечение).
- Волна атрибутируется отдельно (`wave_winning_block_ids`/`wave_cells_won`).
- Партиция держится и с волной, и без неё.
- `apply_ladder`: новый множитель НЕ применяется ретроактивно к раунду,
  который пересёк порог — только со следующего; джамп через несколько
  порогов за раунд начисляет фриспины за КАЖДЫЙ; счёт капается на 36
  (весь экран)."""

from __future__ import annotations

from dataclasses import replace

from bot.services.teto_drillhunt import LADDER
from bot.services.teto_drillhunt import LadderState
from bot.services.teto_drillhunt import apply_drill_hunt
from bot.services.teto_drillhunt import apply_ladder
from bot.services.teto_megablock import GRID_SIZE
from bot.services.teto_megablock import WILD_ID
from bot.services.teto_megablock import MegaBlock
from bot.services.teto_megablock import assert_valid_partition
from bot.services.teto_tumble import TETO_PAYTABLE
from bot.services.teto_tumble import evaluate_paylines
from bot.services.teto_tumble import win_units


class _ForcedRng:
    """См. `tests/test_teto_megablock.py::_ForcedRng`."""

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


# Три РАЗНЫХ не-wild/не-scatter символа «тихой» полосной базы — см. _make_grid.
_STRIPE_SYMBOLS = ("baguette_crumb", "drill_lollipop", "utau_note")


def _make_grid(overrides: dict) -> list[MegaBlock]:
    """Доска 6x6 из одиночных 1x1 блоков, `block_id = row*6 + col`.
    `overrides`: `{(row, col): symbol_id}` для конкретных клеток; остальное —
    столбцовые полосы `_STRIPE_SYMBOLS[col % 3]`. На полных 50 линиях
    (`TETO_PAYLINES`) КАЖДАЯ клетка лежит хотя бы на горизонтали своей строки,
    поэтому однородный филлер (прежний `DEFAULT_FILLER`) выигрывал бы сам по
    себе; полосы же читаются любой линией как S0,S1,S2,S0,S1,S2 — три подряд
    равных не собираются ни на одной линии никакой формы (подробный разбор —
    в докстринге `tests/test_teto_tumble.py::_build_board`). Символ врезки
    обязан отличаться от всех трёх полосных."""
    blocks = []
    for r in range(GRID_SIZE):
        for c in range(GRID_SIZE):
            symbol = overrides.get((r, c), _STRIPE_SYMBOLS[c % 3])
            blocks.append(MegaBlock(block_id=r * 6 + c, symbol_id=symbol, row=r, col=c, height=1, width=1))
    return blocks


def _cell_symbol_map(blocks: list[MegaBlock]) -> dict:
    return {cell: b.symbol_id for b in blocks for cell in b.cells}


def _make_win_after_injection_grid() -> list[MegaBlock]:
    """target = блок (row=2, col=0), block_id=12 (skull_cringe — вне полосных
    символов, поэтому до инъекции ни одна из 50 линий не выигрывает: прогон
    средней горизонтали обрывается на нём же). После инъекции ((2,0) -> WILD)
    средняя горизонталь `TETO_PAYLINES[2]` выигрывает длиной 4 (ids
    12,13,14,15: WILD + три teto_chimera; (2,4) — полосный drill_lollipop —
    обрывает прогон). Что выигрыш РОВНО этот и никакой другой, пинует
    `test_fixture_grids_have_no_pre_existing_win_sanity_check`."""
    return _make_grid({
        (2, 0): "skull_cringe", (2, 1): "teto_chimera",
        (2, 2): "teto_chimera", (2, 3): "teto_chimera",
    })


def _make_no_new_win_grid() -> list[MegaBlock]:
    """target = блок (row=0, col=0), block_id=0. Чистая полосная база: ни до,
    ни после инъекции ((0,0) -> WILD) ни одна линия не выигрывает — wild в
    столбце 0 даёт линиям, начинающимся с него, target = полосный S1
    (drill_lollipop), но третья позиция любой такой линии — уже S2, прогон
    обрывается на длине 2."""
    return _make_grid({})


def test_fixture_grids_have_no_pre_existing_win_sanity_check():
    """Санити обеих фикстур — на 50 линиях это ЕДИНСТВЕННЫЙ заслон от
    «фикстура выигрывает не там, где задумано» (класс бага, вернувшийся при
    расширении 3 -> 50: старые доски, собранные под 3 MVP-линии, начали
    выигрывать на зигзагах/лесенках). Проверяется не только тишина ДО
    инъекции, но и что ПОСЛЕ ручной инъекции Wild выигрыш — в точности тот
    набор блоков, который тесты ниже считают волной (или его нет — для
    no-new-win фикстуры): иначе врезка могла бы молча зацепить вторую линию,
    и `wave_cells_won`/юниты волны проверялись бы против неверной базы."""
    win_grid = _make_win_after_injection_grid()
    has_win, ids = evaluate_paylines(win_grid)
    assert has_win is False
    assert ids == set()

    injected = [replace(b, symbol_id=WILD_ID) if b.block_id == 12 else b for b in win_grid]
    has_win_after, ids_after = evaluate_paylines(injected)
    assert has_win_after is True
    assert ids_after == {12, 13, 14, 15}, "инъекция обязана дать РОВНО прогон средней горизонтали"

    no_win_grid = _make_no_new_win_grid()
    has_win2, _ = evaluate_paylines(no_win_grid)
    assert has_win2 is False
    injected2 = [replace(b, symbol_id=WILD_ID) if b.block_id == 0 else b for b in no_win_grid]
    has_win2_after, _ = evaluate_paylines(injected2)
    assert has_win2_after is False, "wild на (0,0) не должен порождать выигрыш на этой фикстуре"


# ---------------------------------------------------------------------------
# apply_drill_hunt — Вариант C
# ---------------------------------------------------------------------------


def test_apply_drill_hunt_no_infinite_recursion_even_with_forced_repeated_wins(monkeypatch):
    """Наихудший случай: инъекция порождает новый выигрыш, и даже борд ПОСЛЕ
    `resolve_tumble` продолжает выглядеть выигрышным (форсируем через
    monkeypatch адверсариальной заглушкой). Доказываем: `resolve_tumble`
    вызывается РОВНО ОДИН раз, Дрель-Хант не перезапускается сам — это
    структурный факт (один линейный вызов в теле функции, без цикла/рекурсии),
    а не везение форсированного сида."""
    import bot.services.teto_drillhunt as dh_module

    blocks = _make_win_after_injection_grid()
    call_count = {"n": 0}
    still_winning_board = _make_grid({
        (2, 0): "utau_note", (2, 1): "utau_note", (2, 2): "utau_note",
        (2, 3): "utau_note", (2, 4): "utau_note", (2, 5): "utau_note",
    })

    def fake_resolve_tumble(rng_, blocks_, winning_ids_):
        call_count["n"] += 1
        return still_winning_board

    monkeypatch.setattr(dh_module, "resolve_tumble", fake_resolve_tumble)

    rng = _ForcedRng(choices=[12])  # rng.choice(eligible_ids) выбирает id=12=(2,0)
    outcome = apply_drill_hunt(rng, blocks, guaranteed=True, tumble_hard_cap_remaining=5)

    assert outcome.triggered_new_tumble is True
    assert outcome.cells_converted == 1
    assert call_count["n"] == 1, "ключевое доказательство: ровно один вызов resolve_tumble"

    still_win, _ = evaluate_paylines(outcome.blocks)
    assert still_win is True
    assert outcome.blocks is still_winning_board


def test_apply_drill_hunt_respects_tumble_hard_cap_remaining_zero(monkeypatch):
    """`tumble_hard_cap_remaining == 0` — доп. волна НЕ производится, Wild
    остаётся на доске "непроверенным" (потенциальный выигрыш не убирается/не
    гравитируется/не доборается) — осознанное усечение, аналогичное
    `FREESPINS_HARD_CAP`."""
    import bot.services.teto_drillhunt as dh_module

    blocks = _make_win_after_injection_grid()

    def fail_if_called(*a, **kw):
        raise AssertionError("resolve_tumble НЕ должен вызываться при tumble_hard_cap_remaining == 0")

    monkeypatch.setattr(dh_module, "resolve_tumble", fail_if_called)

    rng = _ForcedRng(choices=[12])
    outcome = apply_drill_hunt(rng, blocks, guaranteed=True, tumble_hard_cap_remaining=0)

    assert outcome.triggered_new_tumble is False
    assert outcome.cells_converted == 1  # инъекция произошла — хардкап её не касается

    converted_block = next(b for b in outcome.blocks if b.row == 2 and b.col == 0)
    assert converted_block.symbol_id == WILD_ID

    still_win, _ = evaluate_paylines(outcome.blocks)
    assert still_win is True  # технически всё ещё выигрышно, но не обработано при cap==0


def test_apply_drill_hunt_wave_payout_is_separately_attributable():
    """Выигрыш от доп. волны Дрель-Ханта можно отличить от обычных
    тумбл-выигрышей через `wave_winning_block_ids`/`wave_cells_won`, а его
    ДЕНЬГИ — посчитать по пост-инъекционной доске: `wave_cells_won` — площадь
    (анимация/статистика), деньги же взвешены паутейблом (`win_units`, ровно
    так волну оплачивает `teto_slot_engine._play_one_round`), и внедрённый
    Wild платит как топ-символ (`TETO_PAYTABLE[WILD_ID]`) — денежная
    кульминация Дрель-Ханта. Ожидание собирается ИЗ `TETO_PAYTABLE`
    (Wild-клетка + три teto_chimera, все 1x1), а не магическим числом:
    перекалибровка тарифов не должна ронять тест про атрибуцию."""
    blocks = _make_win_after_injection_grid()
    rng = _ForcedRng(choices=[12] + ["baguette_crumb"] * 10, randints=[7])

    outcome = apply_drill_hunt(rng, blocks, guaranteed=True, tumble_hard_cap_remaining=5)

    assert outcome.triggered_new_tumble is True
    assert outcome.cells_converted == 1
    assert outcome.wave_winning_block_ids == frozenset({12, 13, 14, 15})
    assert outcome.wave_cells_won == 4
    expected_wave_units = TETO_PAYTABLE[WILD_ID] * 1 + 3 * TETO_PAYTABLE["teto_chimera"] * 1
    assert win_units(outcome.blocks_after_injection, outcome.wave_winning_block_ids) == (
        expected_wave_units
    ), "юниты волны обязаны считаться по паутейблу с Wild как топ-символом"
    assert_valid_partition(outcome.blocks)


def test_apply_drill_hunt_conversion_and_optional_wave_preserve_partition_invariant():
    # Сценарий 1: конверсия НЕ создаёт нового выигрыша -> волны нет.
    no_win_blocks = _make_no_new_win_grid()
    rng_a = _ForcedRng(choices=[0])
    outcome_a = apply_drill_hunt(rng_a, no_win_blocks, guaranteed=True, tumble_hard_cap_remaining=5)
    assert outcome_a.triggered_new_tumble is False
    assert outcome_a.cells_converted == 1
    assert_valid_partition(outcome_a.blocks)

    # Сценарий 2: конверсия СОЗДАЁТ новый выигрыш -> доп. волна происходит.
    win_blocks = _make_win_after_injection_grid()
    rng_b = _ForcedRng(choices=[12] + ["baguette_crumb"] * 10, randints=[7])
    outcome_b = apply_drill_hunt(rng_b, win_blocks, guaranteed=True, tumble_hard_cap_remaining=5)
    assert outcome_b.triggered_new_tumble is True
    assert outcome_b.cells_converted == 1
    assert_valid_partition(outcome_b.blocks)


def test_apply_drill_hunt_guaranteed_false_respects_forced_trigger_roll():
    blocks = _make_grid({})

    rng_hit = _ForcedRng(randints=[1], choices=[0])  # randint(1,10)==1 -> сработал
    outcome_hit = apply_drill_hunt(rng_hit, blocks, guaranteed=False, tumble_hard_cap_remaining=0)
    assert outcome_hit.cells_converted == 1
    assert outcome_hit.triggered_new_tumble is False  # cap==0 -> волны нет, но инъекция состоялась

    rng_miss = _ForcedRng(randints=[2])  # randint(1,10)==2 -> НЕ сработал
    outcome_miss = apply_drill_hunt(rng_miss, blocks, guaranteed=False, tumble_hard_cap_remaining=5)
    assert outcome_miss.cells_converted == 0
    assert outcome_miss.triggered_new_tumble is False


# ---------------------------------------------------------------------------
# apply_ladder — Вариант Y
# ---------------------------------------------------------------------------


def test_apply_ladder_single_threshold_crossing_does_not_apply_new_multiplier_to_same_round():
    ladder = LadderState(score=8, multiplier=1)
    new_ladder, extra_fs, final_payout = apply_ladder(ladder, cells_converted=4, raw_round_payout=100)

    assert new_ladder.score == 12
    assert 10 in new_ladder.crossed_thresholds
    assert final_payout == 100, "этот раунд обязан использовать СТАРЫЙ множитель (x1), не новый"
    assert extra_fs == 2
    assert new_ladder.multiplier == 2, "новый множитель зарезервирован на СЛЕДУЮЩИЙ раунд"


def test_apply_ladder_multi_threshold_jump_in_one_round_awards_all_freespins_but_old_multiplier():
    ladder = LadderState(score=4, multiplier=1)
    new_ladder, extra_fs, final_payout = apply_ladder(ladder, cells_converted=18, raw_round_payout=100)

    assert new_ladder.score == 22  # 4 -> 22 пересекает 10, 15, 20 (не 25) за один раунд
    assert new_ladder.crossed_thresholds == {10, 15, 20}
    assert extra_fs == 6  # 3 новых порога * 2
    assert final_payout == 100  # всё ещё множитель на входе в раунд (x1)
    assert new_ladder.multiplier == 5  # достигнутый тир зарезервирован на следующий раунд


def test_apply_ladder_next_round_uses_updated_multiplier():
    ladder_n = LadderState(score=8, multiplier=1)
    ladder_after_n, fs_n, payout_n = apply_ladder(ladder_n, cells_converted=4, raw_round_payout=100)
    assert payout_n == 100
    assert ladder_after_n.multiplier == 2

    ladder_after_n1, fs_n1, payout_n1 = apply_ladder(ladder_after_n, cells_converted=0, raw_round_payout=50)
    assert payout_n1 == 50 * 2 == 100
    assert fs_n1 == 0
    assert ladder_after_n1.multiplier == 2


def test_apply_ladder_score_at_theoretical_max_36_caps_at_x10():
    ladder = LadderState(score=0, multiplier=1)
    new_ladder, extra_fs, final_payout = apply_ladder(ladder, cells_converted=36, raw_round_payout=10)

    assert new_ladder.score == 36
    assert extra_fs == 8  # все 4 порога за один раунд -> 4*2
    assert new_ladder.crossed_thresholds == {10, 15, 20, 25}
    assert new_ladder.multiplier == 10  # таблица лестницы не идёт дальше x10
    assert final_payout == 10  # этот раунд всё ещё на множителе входа (x1)


def test_apply_ladder_exact_threshold_boundary_walk():
    ladder = LadderState(score=9, multiplier=1)
    for threshold, expected_mult in LADDER:
        cells_needed = threshold - ladder.score
        ladder, extra_fs, _payout = apply_ladder(ladder, cells_converted=cells_needed, raw_round_payout=1)
        assert ladder.score == threshold
        assert threshold in ladder.crossed_thresholds
        assert extra_fs == 2
        assert ladder.multiplier == expected_mult


def test_apply_ladder_next_round_application_differs_numerically_from_retroactive_alternative():
    """Численное доказательство различия от отклонённого Варианта X
    (ретроактивное применение в ТОМ ЖЕ раунде, где порог пройден) —
    см. докстринг модуля."""
    ladder = LadderState(score=8, multiplier=1)
    _new_ladder, _extra_fs, final_payout = apply_ladder(ladder, cells_converted=4, raw_round_payout=100)

    retroactive_variant_x_payout_would_be = 100 * 2  # что заплатил бы отклонённый Вариант X в ЭТОМ раунде
    assert final_payout == 100
    assert final_payout != retroactive_variant_x_payout_would_be
