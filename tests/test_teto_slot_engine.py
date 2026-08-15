"""Интеграционные тесты оркестрации слота "Тето Брейнрот"
(`bot/services/teto_slot_engine.py`) — поверх полной сборки (мегаблоки +
тумбл + Дрель-Хант Вариант C + лестница Вариант Y). Не переповторяют юнит-тесты
отдельных компонентов (те уже покрыты `test_teto_megablock.py`,
`test_teto_tumble.py`, `test_teto_drillhunt.py`) — здесь только то, что видно
ТОЛЬКО при полной интеграции.

Plain pytest, без async/БД-фикстур — чистый Python (тот же принцип, что и
`tests/test_slot_engine.py`, только этот слот вообще не трогает `casino_service`
на этом этапе — см. `bot/services/teto_slot_engine.py`)."""

from __future__ import annotations

import json
import random
import time

import bot.services.teto_slot_engine as eng
from bot.services.teto_megablock import ALL_CELLS
from bot.services.teto_megablock import WILD_ID
from bot.services.teto_megablock import assert_valid_partition
from bot.services.teto_megablock import form_blocks
from bot.services.teto_slot_engine import FREESPIN_SCATTER_MIN
from bot.services.teto_slot_engine import FREESPINS_HARD_CAP
from bot.services.teto_slot_engine import TUMBLE_HARD_CAP
from bot.services.teto_slot_engine import _compute_freespins_awarded
from bot.services.teto_slot_engine import play_one_spin


class _ForcedRng:
    """См. `tests/test_teto_megablock.py::_ForcedRng`. Пустой `_ForcedRng()`
    (без настроенных последовательностей) — `.choice(seq)` детерминированно
    возвращает `seq[0]` РЕАЛЬНОГО `seq` на каждом call site, `.randint(a, b)` —
    РЕАЛЬНЫЙ `b`: путь выполнения полностью фиксирован и безопасен от
    `TypeError`/`StopIteration` на ЛЮБОМ call site во всём пайплайне (в
    отличие от произвольно сконфигурированной общей choices-последовательности,
    которая может не совпасть по типу между разными call site — см. находку
    в roadmap.md/отчёте по Тето)."""

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


# ---------------------------------------------------------------------------
# _compute_freespins_awarded — открытая формула
# ---------------------------------------------------------------------------


def test_compute_freespins_awarded_open_formula_not_a_lookup_table():
    """`freespins = scatter_count if scatter_count >= FREESPIN_SCATTER_MIN else
    0` — буквально (порог сейчас 6, поднят с 5 при перекалибровке от
    2026-08-06 после разворота `count_scatter_blocks` на сумму площадей — см.
    её докстринг в `teto_tumble.py`). НЕ 3-кейсовый lookup вроде
    `slot_engine._freespins_for` (тот класс бага уже найден для Azumanga —
    здесь не повторяем, см. `teto_slot_engine.py`)."""
    assert _compute_freespins_awarded(0) == 0
    assert _compute_freespins_awarded(FREESPIN_SCATTER_MIN - 1) == 0
    assert _compute_freespins_awarded(FREESPIN_SCATTER_MIN) == FREESPIN_SCATTER_MIN
    assert _compute_freespins_awarded(16) == 16, "скаттер-мегаблок может закрыть до 16 клеток одним блоком"


# ---------------------------------------------------------------------------
# 1. Идемпотентность/детерминированность полного спина
# ---------------------------------------------------------------------------


def test_full_spin_runs_deterministically_with_forced_rng():
    """Одна и та же конфигурация `_ForcedRng` даёт побайтово идентичный
    результат при двух независимых вызовах `play_one_spin` — движок не должен
    иметь скрытого состояния (вся "память" — только в `rng` и аргументах)."""
    result_1 = play_one_spin(_ForcedRng(), bet_per_line=3)
    result_2 = play_one_spin(_ForcedRng(), bet_per_line=3)

    assert result_1 == result_2
    assert result_1["base_round"]["tumble_steps_used"] >= 1
    assert isinstance(result_1["total_payout"], int)


# ---------------------------------------------------------------------------
# 2. Инвариант партиции держится через ВЕСЬ спин, включая все фриспин-раунды
# ---------------------------------------------------------------------------


def test_partition_invariant_holds_through_entire_spin_including_freespins():
    """500 случайных сидов. Инвариант "sum(area)==36, полное покрытие без
    наложений" обязан держаться ПОСЛЕ КАЖДОГО отдельного шага — класс бага,
    который никакой изолированный юнит-тест одного компонента не мог бы
    поймать (например, если бы Дрель-Хант в сборке передавал в resolve_tumble
    неверный tumble_hard_cap_remaining)."""
    for seed in range(500):
        rng = random.Random(seed)
        trace: list = []
        result = play_one_spin(rng, bet_per_line=1, trace=trace)

        assert len(trace) >= 1, f"seed={seed}: trace пуст"
        for i, entry in enumerate(trace):
            assert_valid_partition(entry["blocks"])

        assert_valid_partition(result["initial_blocks"])
        assert_valid_partition(result["base_round"]["blocks"])
        for rec in result["fs_round_records"]:
            assert_valid_partition(rec["blocks"])
        assert_valid_partition(result["final_blocks"])


# ---------------------------------------------------------------------------
# 3. Оба хардкапа реально останавливают выполнение за конечное время
# ---------------------------------------------------------------------------


def test_tumble_hard_cap_and_freespins_hard_cap_both_respected_simultaneously(monkeypatch):
    """Форсированный патологический сценарий: пустой `_ForcedRng()`
    детерминированно даёт борд из ОДНОГО повторяющегося символа — каждая из 3
    payline всегда выигрывает на каждом шаге тумбла, добор после удаления даёт
    тот же символ — тумбл-каскад никогда не затухает естественно и обязан
    быть остановлен ИСКЛЮЧИТЕЛЬНО `TUMBLE_HARD_CAP`. Дополнительно форсируем
    `_compute_freespins_awarded` на 1000 — `freespins_played` обязан
    остановиться ИСКЛЮЧИТЕЛЬНО `FREESPINS_HARD_CAP`."""
    monkeypatch.setattr(eng, "_compute_freespins_awarded", lambda scatter_count: 1000)

    rng = _ForcedRng()
    t0 = time.perf_counter()
    result = play_one_spin(rng, bet_per_line=1)
    elapsed = time.perf_counter() - t0

    print(
        f"\n[hard-cap stress] elapsed={elapsed:.3f}s, "
        f"freespins_played={result['freespins_played']}, "
        f"base tumble_steps_used={result['base_round']['tumble_steps_used']}"
    )

    assert result["base_round"]["tumble_steps_used"] == TUMBLE_HARD_CAP
    assert result["base_round"]["tumble_hard_cap_hit"] is True
    assert len(result["fs_round_records"]) > 0
    for rec in result["fs_round_records"]:
        assert rec["tumble_steps_used"] == TUMBLE_HARD_CAP
        assert rec["tumble_hard_cap_hit"] is True

    assert result["freespins_awarded"] == 1000
    assert result["freespins_played"] == FREESPINS_HARD_CAP
    assert result["freespins_hard_cap_hit"] is True, "хардкап реально остановил цикл раньше естественного исчерпания"
    assert len(result["fs_round_records"]) == FREESPINS_HARD_CAP

    assert elapsed < 10.0, f"патологический сценарий занял {elapsed:.2f}s — подозрение на runaway"


# ---------------------------------------------------------------------------
# 4. Дрель-Хант никогда не срабатывает раньше, чем тумбл-каскад исчерпан
# ---------------------------------------------------------------------------


def test_drill_hunt_never_fires_before_tumble_cascade_exhausted(monkeypatch):
    """Структурная проверка ПОРЯДКА вызовов, 200 случайных сидов: каждый
    вызов `apply_drill_hunt` обязан следовать сразу за таким
    `evaluate_paylines`, который вернул `has_win=False`."""
    from bot.services import teto_tumble

    call_log: list[tuple[str, bool | None]] = []

    real_evaluate = teto_tumble.evaluate_paylines
    real_drill = eng.apply_drill_hunt

    def spy_evaluate(blocks):
        has_win, winning_ids = real_evaluate(blocks)
        call_log.append(("evaluate_paylines", has_win))
        return has_win, winning_ids

    def spy_drill(rng, blocks, *, guaranteed, tumble_hard_cap_remaining):
        call_log.append(("apply_drill_hunt", None))
        return real_drill(rng, blocks, guaranteed=guaranteed, tumble_hard_cap_remaining=tumble_hard_cap_remaining)

    monkeypatch.setattr(eng, "evaluate_paylines", spy_evaluate)
    monkeypatch.setattr(eng, "apply_drill_hunt", spy_drill)

    for seed in range(200):
        call_log.clear()
        rng = random.Random(seed)
        eng.play_one_spin(rng, bet_per_line=1)

        assert call_log, f"seed={seed}: ни одного вызова не залогировано"
        last_eval_has_win = None
        saw_any_drill_call = False
        for kind, val in call_log:
            if kind == "evaluate_paylines":
                last_eval_has_win = val
            elif kind == "apply_drill_hunt":
                saw_any_drill_call = True
                assert last_eval_has_win is False, (
                    f"seed={seed}: apply_drill_hunt вызван при has_win={last_eval_has_win}"
                )
        assert saw_any_drill_call, f"seed={seed}: apply_drill_hunt ни разу не вызван"


# ---------------------------------------------------------------------------
# 5. WILD никогда не появляется в обычном заполнении реела
# ---------------------------------------------------------------------------


def test_wild_symbol_never_appears_except_via_drill_hunt():
    """1000 случайных базовых заполнений (`form_blocks` напрямую, ДО любого
    Дрель-Ханта) — `WILD_ID` не должен встретиться НИ РАЗУ."""
    for seed in range(1000):
        rng = random.Random(seed)
        blocks = form_blocks(rng, set(ALL_CELLS))
        for b in blocks:
            assert b.symbol_id != WILD_ID, f"seed={seed}: WILD_ID найден в обычном заполнении"


# ---------------------------------------------------------------------------
# 6. Один полностью форсированный нетривиальный спин — читаемый лог
# ---------------------------------------------------------------------------


def test_forced_end_to_end_nontrivial_spin_matches_expected_shape():
    """ОДИН полностью детерминированный сценарий (форс-сид 38786, найден
    перебором сидов 0..60k ПОСЛЕ перекалибровки от 2026-08-06: разворот
    `count_scatter_blocks` на сумму площадей + новые `WEIGHTS`
    (скаттер 1/42, был 1/27) + `FREESPIN_SCATTER_MIN=6` (был 5) сдвинули
    потребление RNG и сделали триггер фриспинов заметно реже (~1 к 420-450
    вместо ~1 к 80-100), поэтому прежний сид 98182 перестал давать фриспины —
    новый сид обязан переподбираться после КАЖДОЙ правки
    WEIGHTS/SHAPE_POOL/TETO_PAYLINES/FREESPIN_SCATTER_MIN, потому что любая из
    них сдвигает потребление RNG), где заведомо происходит: мегаблок в
    первичном заполнении + хотя бы один тумбл-шаг + Дрель-Хант в базовой игре +
    начисление фриспинов + Дрель-Хант с волной хотя бы в одном фриспин-раунде
    + пересечение хотя бы одного порога лестницы."""
    seed = 38786
    rng = random.Random(seed)
    result = play_one_spin(rng, bet_per_line=1)

    initial_blocks = result["initial_blocks"]
    base = result["base_round"]

    had_mega_block_in_initial_fill = any(b.area >= 4 for b in initial_blocks)
    base_tumble_happened = base["tumble_steps_used"] >= 1
    base_drill_hunt_fired = base["drill_hunt_fired"]
    freespins_awarded = result["freespins_awarded"]
    any_fs_wave_fired = any(rec["drill_hunt_wave_fired"] for rec in result["fs_round_records"])
    ladder = result["ladder_final_state"]
    ladder_crossed_a_threshold = len(ladder.crossed_thresholds) >= 1

    assert had_mega_block_in_initial_fill, "ожидался мегаблок в первичном заполнении"
    assert base_tumble_happened, "ожидался хотя бы один тумбл-шаг в базовой игре"
    assert base_drill_hunt_fired, "ожидался сработавший Дрель-Хант в базовой игре"
    assert freespins_awarded > 0, "ожидалось начисление фриспинов"
    assert any_fs_wave_fired, "ожидалась хотя бы одна волна Дрель-Ханта во фриспинах"
    assert ladder_crossed_a_threshold, "ожидалось пересечение хотя бы одного порога лестницы"

    assert_valid_partition(result["final_blocks"])

    total_cells_converted = base["cells_converted_by_drill_hunt"] + sum(
        rec["cells_converted_by_drill_hunt"] for rec in result["fs_round_records"]
    )

    print("\n" + "=" * 70)
    print(f"FORCED END-TO-END SPIN (seed={seed}, bet_per_line=1)")
    print("=" * 70)
    print(f"Initial fill had mega-block: {had_mega_block_in_initial_fill}")
    print(
        f"Base round: tumble_steps_used={base['tumble_steps_used']}, "
        f"drill_hunt_fired={base['drill_hunt_fired']}, "
        f"cells_converted={base['cells_converted_by_drill_hunt']}, "
        f"raw_round_payout={base['raw_round_payout']}"
    )
    print(f"Scatter count after base round: {result['scatter_count']}")
    print(f"Freespins awarded: {freespins_awarded}, freespins played: {result['freespins_played']}")
    print("-" * 70)
    for rec in result["fs_round_records"]:
        print(
            f"  FS round {rec['round_index']:>3}: tumble_steps={rec['tumble_steps_used']:>2} "
            f"wave_fired={str(rec['drill_hunt_wave_fired']):>5} "
            f"cells_converted={rec['cells_converted_by_drill_hunt']} "
            f"raw_payout={rec['raw_round_payout']:>4} final_payout={rec['final_round_payout']:>4} "
            f"ladder(score={rec['ladder_score_after']:>2}, mult=x{rec['ladder_multiplier_after']}) "
            f"extra_fs=+{rec['extra_freespins_awarded_this_round']}"
        )
    print("-" * 70)
    print(f"TOTAL cells converted by Drill-Hunt across whole spin: {total_cells_converted}")
    print(
        f"Final ladder state: score={ladder.score}, multiplier=x{ladder.multiplier}, "
        f"crossed_thresholds={sorted(ladder.crossed_thresholds)}"
    )
    print(f"TOTAL PAYOUT: {result['total_payout']}")
    print("=" * 70)


# ---------------------------------------------------------------------------
# 7. Лестница живёт ТОЛЬКО во фриспинах — очки базового Дрель-Ханта в неё не идут
# ---------------------------------------------------------------------------


def test_base_round_drill_hunt_cells_never_feed_the_ladder_score(monkeypatch):
    """ЗАФИКСИРОВАНО владельцем бота 2026-07-30 (см. `teto_slot_engine.py`,
    строка `ladder = LadderState()`): лестница множителя — фича ТОЛЬКО
    фриспинов, `LadderState` создаётся заново на вход во фриспины, очки
    Дрель-Ханта из БАЗОВОГО (не фриспин) раунда в неё не идут. Прямой
    регрессионный тест на этот инвариант — без него правка вроде
    `LadderState(score=base_result["cells_converted_by_drill_hunt"])` прошла
    бы незамеченной всем остальным набором.

    Форсируем apply_drill_hunt на ПЕРВЫЙ вызов (базовый раунд) вернуть
    заведомо большой cells_converted (30 — намеренно больше первого порога
    лестницы 10, чтобы утечка была однозначно детектируема), делегируя
    реальной реализации на всех последующих вызовах (фриспин-раунды), чтобы
    остальная механика оставалась настоящей."""
    from bot.services import teto_drillhunt

    real_apply_drill_hunt = teto_drillhunt.apply_drill_hunt
    call_count = {"n": 0}

    def spy_apply_drill_hunt(rng, blocks, *, guaranteed, tumble_hard_cap_remaining):
        call_count["n"] += 1
        outcome = real_apply_drill_hunt(
            rng, blocks, guaranteed=guaranteed, tumble_hard_cap_remaining=tumble_hard_cap_remaining
        )
        if call_count["n"] == 1:
            # Первый вызов apply_drill_hunt за весь спин — ВСЕГДА базовый
            # раунд (play_one_spin вызывает _play_one_round(guaranteed=False)
            # для базы ДО цикла фриспинов, см. teto_slot_engine.py).
            outcome = outcome._replace(cells_converted=30)
        return outcome

    monkeypatch.setattr(eng, "apply_drill_hunt", spy_apply_drill_hunt)
    monkeypatch.setattr(eng, "_compute_freespins_awarded", lambda scatter_count: 3)  # гарантируем фриспины

    rng = random.Random(12345)
    result = play_one_spin(rng, bet_per_line=1)

    assert result["base_round"]["cells_converted_by_drill_hunt"] == 30, "форс не применился к базовому раунду"
    assert result["fs_round_records"], "ожидались фриспин-раунды (форсировали _compute_freespins_awarded)"

    first_fs_round = result["fs_round_records"][0]
    assert first_fs_round["ladder_score_after"] == first_fs_round["cells_converted_by_drill_hunt"], (
        "счёт лестницы после ПЕРВОГО фриспин-раунда обязан включать ТОЛЬКО очки "
        "этого раунда (без форсированных 30 клеток базового раунда) — иначе "
        "лестница ошибочно унаследовала счёт из базовой игры"
    )


# ---------------------------------------------------------------------------
# 8. Трейс анимации: схема, грамматика, деньги, нумерация шагов, предел размера
# ---------------------------------------------------------------------------
#
# Всё ниже проверяет ПРЕЗЕНТАЦИОННЫЙ выход движка (`trace` +
# `serialize_animation`), а не деньги: деньги уже покрыты тестами выше и в
# `test_casino_service.py`. Ценность этих тестов — в том, что каждый из них
# прямая регрессия на КОНКРЕТНЫЙ дефект старого трейса, из-за которого
# анимированный игровой экран было невозможно построить (см. докстринги
# `teto_slot_engine._record` / `_run_tumble_cascade` / `serialize_animation`).

# Ключи, специфичные для каждого op'а (сверх общих `op`/`seq`/`phase`/`round`
# и всегда-последнего `blocks`). Правило фиксированной формы: op НИКОГДА не
# опускает свой ключ, неприменимое значение — None/[]/0.
_OP_SPECIFIC_KEYS = {
    "fill": set(),
    "evaluate": {"source", "has_win", "winning_block_ids", "winning_lines", "cells_won", "payout"},
    "tumble": {"source", "step", "removed_block_ids"},
    "drill_hunt": {"fired", "wild_block_id", "cells_converted", "wave_fired"},
    "round_end": {
        "raw_round_payout", "multiplier_applied", "final_round_payout",
        "cells_converted_by_drill_hunt", "tumble_steps_used", "tumble_hard_cap_hit",
        "scatter_count", "freespins_awarded", "ladder",
    },
}


def _group_rounds(trace: list) -> list[list[dict]]:
    """Разбивает плоский трейс на раунды по (`phase`, `round`) — тем же
    способом, что `serialize_animation` и любой потребитель: сравнением с
    ПРЕДЫДУЩИМ op'ом, без сортировок и без парсинга имён. Если бы этот хелпер
    оказался сложнее трёх строк, это само было бы признаком, что сегментация
    из трейса не читается."""
    groups: list[list[dict]] = []
    last_key = None
    for op in trace:
        key = (op["phase"], op["round"])
        if key != last_key:
            groups.append([])
            last_key = key
        groups[-1].append(op)
    return groups


# Три РАЗНЫХ линейных символа «тихой» полосной базы фикстур (см. _make_grid);
# на этот же кортеж опирается цикл добора в _WildOnBlock12Rng ниже.
_STRIPE_SYMBOLS = ("baguette_crumb", "drill_lollipop", "utau_note")


def _make_grid(overrides: dict) -> list:
    """Локальная копия `tests/test_teto_drillhunt.py::_make_grid` (конвенция
    этого набора — стабы/фикстуры дублируются в каждом тестовом модуле):
    доска 6x6 из одиночных 1x1 блоков, `block_id = row*6 + col`. База —
    столбцовые полосы `_STRIPE_SYMBOLS[col % 3]`: на полных 50 линиях каждая
    payline читает S0,S1,S2,S0,S1,S2 и не собирает трёх подряд ни на одной
    форме линии (подробный разбор — `tests/test_teto_tumble.py::_build_board`);
    символ врезки обязан отличаться от всех трёх полосных."""
    from bot.services.teto_megablock import GRID_SIZE
    from bot.services.teto_megablock import MegaBlock

    return [
        MegaBlock(
            block_id=r * GRID_SIZE + c,
            symbol_id=overrides.get((r, c), _STRIPE_SYMBOLS[c % 3]),
            row=r, col=c, height=1, width=1,
        )
        for r in range(GRID_SIZE)
        for c in range(GRID_SIZE)
    ]


def _make_win_after_injection_grid() -> list:
    """Копия `tests/test_teto_drillhunt.py::_make_win_after_injection_grid`:
    ДО инъекции ни одна из 50 линий не выигрывает; после конверсии блока 12
    (клетка (2,0), skull_cringe) в WILD средняя горизонталь `TETO_PAYLINES[2]`
    выигрывает длиной 4 (ids 12,13,14,15: WILD + три teto_chimera; (2,4) —
    полосный drill_lollipop — обрывает прогон). Точность врезки («выигрыш
    ровно этот и только он») пинует
    `tests/test_teto_drillhunt.py::test_fixture_grids_have_no_pre_existing_win_sanity_check`."""
    return _make_grid({
        (2, 0): "skull_cringe", (2, 1): "teto_chimera",
        (2, 2): "teto_chimera", (2, 3): "teto_chimera",
    })


class _WildOnBlock12Rng:
    """RNG-стаб, различающий call site'ы ПО ТИПУ элементов последовательности,
    а не по порядку вызовов: `choice` над списком int'ов — это всегда
    `eligible_ids` внутри `apply_drill_hunt` (возвращаем 12, т.е. клетку
    (2,0)), над списком символов (единственный такой call site здесь —
    `FILL_POOL` при доборе) — цикл по `_STRIPE_SYMBOLS`; `randint(a, b)` —
    всегда `b`.

    Почему не общий `_ForcedRng(choices=[12, ...])`: тот циклит ОДНУ
    последовательность по всем call site'ам, поэтому после выбора блока начал
    бы подставлять `12` как `symbol_id` при доборе в `form_blocks` — ровно тот
    класс несовпадения типов между call site'ами, о котором предупреждает
    докстринг `_ForcedRng` выше. Дифференциация по типу делает путь исполнения
    фиксированным независимо от того, сколько раз каскад вызовет добор.

    Почему добор — ЦИКЛ по `_STRIPE_SYMBOLS`, а не константный `seq[0]` (как
    делает `_ForcedRng`): волна снимает клетки (2,0)..(2,3), добор кладёт 4
    новые одиночки в (0,0)..(0,3) (`form_blocks` идёт по sorted-клеткам), и
    константный символ собрал бы прогон 4 на ВЕРХНЕЙ горизонтали — на полном
    наборе 50 линий это незатухающий каскад до самого `TUMBLE_HARD_CAP`
    вместо сценария «ровно одна волна». Цикл периода 3, стартующий на столбце
    0, кладёт в столбцы 0..3 ровно `_STRIPE_SYMBOLS[col % 3]`, т.е. буквально
    ВОССТАНАВЛИВАЕТ полосную защиту фикстуры — каскад после волны затухает
    немедленно. `randint -> b` попутно означает `randint(1, 10) == 10 != 1`,
    т.е. в базовом раунде Дрель-Хант НЕ срабатывает (нужно для чистоты
    сценария) и добор никогда не пытается положить мегаблок."""

    def __init__(self):
        self._fill_i = 0

    def choice(self, seq):
        if seq and isinstance(seq[0], int):
            return 12
        v = _STRIPE_SYMBOLS[self._fill_i % len(_STRIPE_SYMBOLS)]
        self._fill_i += 1
        return v

    def randint(self, a, b):
        return b


def test_trace_is_pure_observation_traced_and_untraced_spins_are_identical():
    """400 сидов. `play_one_spin(rng, bet)` и `play_one_spin(rng, bet,
    trace=[])` на ОДИНАКОВО засеянном rng обязаны дать побайтово идентичный
    результат: трейс — наблюдатель, а не участник.

    Почему это самый важный тест всей фичи, а не формальность. В проде ЖИВЫ
    ОБА ПУТИ одновременно: HTTP-роут всегда передаёт sink (спин идёт
    ТРАССИРУЕМЫМ), а `play_teto_slots` без `animation_sink` (бот-команда,
    любой будущий вызывающий) — НЕТРАССИРУЕМЫМ. Если запись кадров начнёт
    хоть как-то влиять на исход, две двери в одну и ту же игру начнут платить
    разные деньги — и это НЕ гипотетика: мутация, обернувшая денежную строку
    `total_cells +=` в `_run_tumble_cascade` в `if trace is not None:`,
    оставляла ВЕСЬ остальной набор зелёным, при этом на сиде 8 нетрассируемый
    спин платил 0, а трассируемый — 9, и расходились 43 из 300 сидов. Три
    точных теста движка (`..._with_forced_rng`, `..._nontrivial_spin_...`,
    `..._never_feed_the_ladder_score`) этого не ловили: все три зовут
    НЕТРАССИРУЕМЫЙ путь, которым прод как раз и не ходит.

    Сравниваются `serialize_spin_result`-формы, а не сырые dict'ы: так
    сравнение включает и доски (расхождение потребления RNG видно как другая
    партиция), и лестницу, и все выплаты, и при этом читаемо в diff'е pytest.
    `bet_per_line=3` (не 1) — чтобы расхождение в клетках было видно и после
    умножения, а не тонуло в единичном множителе."""
    saw_paying_spin = False
    for seed in range(400):
        untraced = eng.serialize_spin_result(play_one_spin(random.Random(seed), bet_per_line=3))
        traced = eng.serialize_spin_result(
            play_one_spin(random.Random(seed), bet_per_line=3, trace=[])
        )

        assert untraced["total_payout"] == traced["total_payout"], (
            f"seed={seed}: трассируемый спин заплатил {traced['total_payout']}, "
            f"нетрассируемый — {untraced['total_payout']}; трейс обязан быть "
            "чистым наблюдателем, а не участником расчёта"
        )
        assert untraced == traced, f"seed={seed}: трассируемый и нетрассируемый спины разошлись"
        if untraced["total_payout"] > 0:
            saw_paying_spin = True

    assert saw_paying_spin, (
        "во всей выборке не встретилось ни одного спина с ненулевой выплатой — "
        "сравнение выплат было проверено вхолостую"
    )


def test_block_ids_are_unique_across_the_whole_spin_not_just_within_a_round():
    """8000 сидов (было 200 — недостаточно ПОСЛЕ перекалибровки от 2026-08-06:
    триггер фриспинов упал с ~1 к 80-100 до ~1 к 420-450, поэтому на 200
    сидах P(ни одного многораундового спина) ~= exp(-200/430) ~= 0.63 —
    тест был бы флейки-провальным чаще, чем зелёным; 8000 сидов дают
    ожидаемо ~18-19 триггеров, P(нуля) исчезающе мал). `block_id` не
    переиспользуется между раундами одного спина: множества id любых двух
    РАЗНЫХ раундов трейса не пересекаются.

    Зачем это свойство (см. `teto_slot_engine._next_block_id`): `block_id` —
    единственный стабильный идентификатор блока между кадрами, по нему
    анимация твинит движение. Раньше каждый раунд звал `form_blocks(...)` с
    дефолтным `start_id=0`, поэтому id начинались заново, и фронт, диффающий
    соседние кадры (а `fill` следующего раунда стоит в массиве СРАЗУ ЗА
    `round_end` предыдущего), связал бы блок 7 первого раунда с совершенно
    другим блоком 7 второго и нарисовал бы переезд несуществующего блока."""
    saw_multi_round_spin = False
    for seed in range(8000):
        trace: list = []
        play_one_spin(random.Random(seed), bet_per_line=1, trace=trace)

        per_round = [
            {b.block_id for op in round_ops for b in op["blocks"]}
            for round_ops in _group_rounds(trace)
        ]
        if len(per_round) >= 2:
            saw_multi_round_spin = True
        for i in range(len(per_round)):
            for j in range(i + 1, len(per_round)):
                assert not (per_round[i] & per_round[j]), (
                    f"seed={seed}: id {sorted(per_round[i] & per_round[j])[:5]} "
                    f"встречаются и в раунде {i}, и в раунде {j}"
                )

    assert saw_multi_round_spin, "в выборке не было ни одного спина с фриспинами — проверять было нечего"


def test_round_end_ladder_carries_the_target_a_progress_gauge_needs():
    """8000 сидов (было 200 — та же причина, что у теста block_id выше:
    триггер фриспинов после перекалибровки от 2026-08-06 упал до ~1 к
    420-450, 200 сидов больше не дают статистической гарантии хотя бы одного
    фриспин-раунда). `round_end.ladder` фриспин-раунда несёт не только
    пройденное (`score_after`/`multiplier_after`/`crossed_thresholds`), но и
    ЦЕЛЬ (`next_threshold`/`next_multiplier`/`score_to_next`), а сам список
    порогов и потолок счёта лежат в конверте
    (`ladder_thresholds`/`ladder_max_score`).

    Без этого шкала-вал дрели — заявленный "запоминающийся приём" экрана
    (`docs/teto-slot-design-direction.md` §4) — не рисуется вовсе: прогресс-бар
    нельзя построить, зная только сколько набрано, но не зная, до чего тянуть.
    Единственная альтернатива была бы копией порогов на клиенте, т.е. вторым
    источником истины, который разъедется при первой перекалибровке."""
    from bot.services.teto_drillhunt import LADDER
    from bot.services.teto_drillhunt import MAX_BOARD_SCORE

    saw_open_gauge = False
    saw_completed_ladder = False
    for seed in range(8000):
        trace: list = []
        result = play_one_spin(random.Random(seed), bet_per_line=1, trace=trace)
        envelope = eng.serialize_animation(trace, paid_total=result["total_payout"])

        assert envelope["ladder_thresholds"] == [
            {"score": score, "multiplier": mult} for score, mult in LADDER
        ]
        assert envelope["ladder_max_score"] == MAX_BOARD_SCORE

        for op in trace:
            if op["op"] != "round_end" or op["ladder"] is None:
                continue
            ladder = op["ladder"]
            assert set(ladder) == {
                "score_after", "multiplier_after", "crossed_thresholds",
                "extra_freespins_awarded", "next_threshold", "next_multiplier",
                "score_to_next",
            }
            if ladder["next_threshold"] is None:
                # Все пороги пройдены — цели больше нет, и это выражено
                # СОГЛАСОВАННО во всех трёх полях (None, а не 0/последний порог).
                assert ladder["next_multiplier"] is None
                assert ladder["score_to_next"] is None
                assert len(ladder["crossed_thresholds"]) == len(LADDER)
                saw_completed_ladder = True
            else:
                saw_open_gauge = True
                assert ladder["next_threshold"] not in ladder["crossed_thresholds"]
                assert ladder["score_to_next"] == max(
                    0, ladder["next_threshold"] - ladder["score_after"]
                )
                assert ladder["score_to_next"] >= 0, "отрицательный остаток шкала нарисует наоборот"
                # Цель — СЛЕДУЮЩИЙ по порядку непройденный порог, а не любой.
                assert ladder["next_threshold"] == min(
                    score for score, _m in LADDER if score not in ladder["crossed_thresholds"]
                )

    assert saw_open_gauge, "не встретилось ни одного раунда с недостигнутым порогом"
    if not saw_completed_ladder:
        print(
            "\n[test_round_end_ladder_carries_the_target...] за 200 сидов лестница ни разу "
            "не закрылась целиком — ветка 'цели больше нет' проверена только форсом ниже"
        )
    # Ветка "все пороги пройдены" не гарантируется случайной выборкой — форсим её явно.
    from bot.services.teto_drillhunt import LadderState
    from bot.services.teto_drillhunt import ladder_gauge

    full = LadderState(score=MAX_BOARD_SCORE, multiplier=10, crossed_thresholds={s for s, _m in LADDER})
    assert ladder_gauge(full) == {
        "next_threshold": None, "next_multiplier": None, "score_to_next": None
    }


def test_serialize_animation_never_lets_the_screen_count_past_what_was_paid():
    """Денежный контракт конверта — прямая регрессия на инцидент Azumanga
    ("/me до и после раунда — 1000 и 1000, банк чата был 0"), перенесённый на
    Тето, где он громче: экран Тето ТИКАЕТ счётчик по раундам.

    Движковый итог спина (`outcome.total_payout`, он же сумма
    `round_end.final_round_payout`) — это выплата ДО капа банком
    (`economy_service.pay_from_bank`, D-06). Реально игрок получает
    `min(payout, остаток банка)`. Конверт обязан нести ОБА числа и явный флаг,
    иначе счётчик досчитывает до суммы, которой не было.

    Проверяем ровно ту формулу, которая описана как единственно правильная в
    докстринге `serialize_animation`: показанный итог после раунда k ==
    `min(префиксная сумма final_round_payout, payout_paid)` — она никогда не
    превышает выплаченное, монотонна и заканчивается РОВНО на `payout_paid`.

    Сид 4 (был 12945 — перестал платить после перекалибровки WEIGHTS/
    TETO_PAYTABLE от 2026-08-06, сдвинувшей потребление RNG базового
    заполнения; переподобран как первый winning-сид от 0)."""
    trace: list = []
    result = play_one_spin(random.Random(4), bet_per_line=10, trace=trace)
    engine_total = result["total_payout"]
    assert engine_total > 0, "сид подобран как выигрышный — если это изменилось, подобрать новый"

    # (1) банк вместил всё: капа нет, оба числа совпадают.
    full = eng.serialize_animation(trace, paid_total=engine_total)
    assert full["payout_paid"] == full["payout_engine_total"] == engine_total
    assert full["bank_capped"] is False

    # (2) банк вместил лишь часть — ровно тот случай, из-за которого экран врал.
    paid = engine_total // 4
    assert paid < engine_total
    capped = eng.serialize_animation(trace, paid_total=paid)
    assert capped["bank_capped"] is True
    assert capped["payout_paid"] == paid
    assert capped["payout_engine_total"] == engine_total, (
        "движковый итог обязан остаться ЧЕСТНЫМ — фальсифицировать его, чтобы "
        "сошёлся с выплатой, значит соврать в другую сторону"
    )

    for envelope in (full, capped):
        # Формула ниже сходится к `payout_paid` только на ПОЛНОМ трейсе —
        # фиксируем, что этот спин не усечён (иначе тест проверял бы не то,
        # что написано в его названии; контракт усечённого показа — отдельный).
        assert envelope["truncated"] is False
        shown = 0
        running = 0
        for round_ops in _group_rounds(envelope["ops"]):
            running += round_ops[-1]["final_round_payout"]
            shown = min(running, envelope["payout_paid"])
            assert shown <= envelope["payout_paid"], "счётчик перевалил за выплаченное"
        assert shown == envelope["payout_paid"], (
            "счётчик обязан ЗАКАНЧИВАТЬСЯ ровно на выплаченном — иначе игрок видит "
            "одно число на экране и другое в балансе"
        )
        # Сырые суммы раундов при капе действительно больше выплаты — это не
        # баг данных, а сам факт капа, ради которого формула и нужна.
        assert running == envelope["payout_engine_total"]


def test_trace_schema_conformance_and_no_round_number_in_any_op_name():
    """300 сидов. Каждый op несёт РОВНО свой набор ключей (общие +
    op-специфичные + `blocks` последним), `op` — из фиксированного словаря
    пяти значений, `seq` == индексу в массиве.

    Главная регрессия здесь — последний assert: НИ ОДНО имя op'а не содержит
    цифры. Старый трейс кодировал номер раунда в строке имени
    (`fs_round_7_initial_fill`), из-за чего потребителю приходилось парсить
    имена регуляркой и догадываться о сегментации; проверка "в имени нет
    цифр" — самый прямой способ не дать этому вернуться незамеченным."""
    for seed in range(300):
        trace: list = []
        play_one_spin(random.Random(seed), bet_per_line=2, trace=trace)

        assert trace, f"seed={seed}: трейс пуст"
        for i, op in enumerate(trace):
            name = op["op"]
            assert name in eng.TRACE_OPS, f"seed={seed}: неизвестный op {name!r}"
            assert not any(ch.isdigit() for ch in name), (
                f"seed={seed}: имя op'а {name!r} содержит цифру — идентичность раунда "
                "обязана жить в структурных phase/round, а не в строке имени"
            )
            assert op["seq"] == i, f"seed={seed}: seq={op['seq']} != индекса {i}"

            expected = {"op", "seq", "phase", "round", "blocks"} | _OP_SPECIFIC_KEYS[name]
            assert set(op) == expected, (
                f"seed={seed}, op={name}: набор ключей {sorted(set(op) ^ expected)} расходится"
            )
            keys = list(op)
            assert keys[:4] == ["op", "seq", "phase", "round"], f"seed={seed}: порядок ключей"
            assert keys[-1] == "blocks", f"seed={seed}: blocks обязан быть последним ключом"


def test_trace_grammar_rounds_open_with_fill_and_close_with_round_end():
    """200 сидов. Грамматика раунда: `fill ... round_end`, `phase == "base"`
    <=> `round == 0`, фриспин-раунды нумеруются 1..N по возрастанию, а число
    раундов в трейсе == `1 + freespins_played`.

    Это то, что делает "простейший корректный потребитель" возможным: идти по
    `ops` по порядку, `fill` открывает раунд, `round_end` закрывает — без
    строкового разбора, без эвристик сегментации."""
    for seed in range(200):
        trace: list = []
        result = play_one_spin(random.Random(seed), bet_per_line=1, trace=trace)

        rounds = _group_rounds(trace)
        assert len(rounds) == 1 + result["freespins_played"], (
            f"seed={seed}: раундов в трейсе {len(rounds)}, "
            f"а сыграно 1 + {result['freespins_played']}"
        )

        for round_ops in rounds:
            assert round_ops[0]["op"] == "fill", f"seed={seed}: раунд не открылся fill"
            assert round_ops[-1]["op"] == "round_end", f"seed={seed}: раунд не закрылся round_end"
            phases = {op["phase"] for op in round_ops}
            indices = {op["round"] for op in round_ops}
            assert len(phases) == 1 and len(indices) == 1, f"seed={seed}: раунд не однороден"
            phase = phases.pop()
            index = indices.pop()
            assert (phase == "base") == (index == 0), (
                f"seed={seed}: phase={phase} и round={index} должны совпадать по смыслу"
            )

        assert rounds[0][0]["phase"] == "base"
        fs_indices = [ops[0]["round"] for ops in rounds[1:]]
        assert fs_indices == list(range(1, len(rounds))), f"seed={seed}: {fs_indices}"

        # Базовый `fill` — та же доска, что `result["initial_blocks"]`
        # (первичное заполнение видно фронту ровно тем же кадром).
        assert rounds[0][0]["blocks"] == result["initial_blocks"]
        assert rounds[0][-1]["blocks"] == result["base_round"]["blocks"]
        for round_ops, rec in zip(rounds[1:], result["fs_round_records"]):
            assert round_ops[-1]["blocks"] == rec["blocks"]
            assert round_ops[-1]["round"] == rec["round_index"]
            assert round_ops[-1]["ladder"]["extra_freespins_awarded"] == (
                rec["extra_freespins_awarded_this_round"]
            )


def test_trace_tumble_removes_exactly_what_the_preceding_evaluate_highlighted():
    """200 сидов. Требование "знать, что умрёт, ДО того как оно умрёт":
    `tumble.removed_block_ids` == `winning_block_ids` НЕПОСРЕДСТВЕННО
    предшествующего `evaluate`, и ни один из этих id не встречается в доске
    самого `tumble` (они уже удалены гравитацией).

    Старый op `evaluate_paylines` не сообщал победивших блоков ВООБЩЕ никак —
    подсветить/взорвать нужные блоки перед их исчезновением было нечем."""
    for seed in range(200):
        trace: list = []
        play_one_spin(random.Random(seed), bet_per_line=1, trace=trace)

        for i, op in enumerate(trace):
            if op["op"] != "tumble":
                continue
            prev = trace[i - 1]
            assert prev["op"] == "evaluate", f"seed={seed}: перед tumble не evaluate, а {prev['op']}"
            assert prev["has_win"] is True
            assert prev["winning_block_ids"], f"seed={seed}: выигрыш без id"
            assert op["removed_block_ids"] == prev["winning_block_ids"], (
                f"seed={seed}: tumble удаляет не то, что подсветил evaluate"
            )
            assert op["source"] == prev["source"], f"seed={seed}: source пары evaluate/tumble разошёлся"

            survivors = {b.block_id for b in op["blocks"]}
            assert not (set(op["removed_block_ids"]) & survivors), (
                f"seed={seed}: удалённые id всё ещё на доске после tumble"
            )
            # Выигравшие блоки ОБЯЗАНЫ присутствовать на кадре evaluate — это и
            # есть кадр, на котором анимация их подсвечивает.
            highlighted = {b.block_id for b in prev["blocks"]}
            assert set(prev["winning_block_ids"]) <= highlighted


def test_trace_money_ties_out_per_round_and_over_the_whole_spin():
    """200 сидов, ставка на линию 3 (не 1 — иначе умножение на множитель
    лестницы было бы невидимо в арифметике). Три инварианта:
      - сумма `evaluate.payout` по раунду == `round_end.raw_round_payout`
        (сырые, ДО множителя — фронт, умножающий на каждом шаге, удвоил бы);
      - `final_round_payout == raw_round_payout * multiplier_applied`;
      - сумма `final_round_payout` по всем раундам == `result["total_payout"]`.
    Последний — прямая защита от расхождения между тем, что показала анимация,
    и тем, что реально начислено игроку."""
    for seed in range(200):
        trace: list = []
        result = play_one_spin(random.Random(seed), bet_per_line=3, trace=trace)

        total = 0
        for round_ops in _group_rounds(trace):
            end = round_ops[-1]
            evaluated = sum(op["payout"] for op in round_ops if op["op"] == "evaluate")
            assert evaluated == end["raw_round_payout"], (
                f"seed={seed}, раунд {end['round']}: сумма evaluate.payout={evaluated} "
                f"!= raw_round_payout={end['raw_round_payout']}"
            )
            assert end["final_round_payout"] == end["raw_round_payout"] * end["multiplier_applied"]
            total += end["final_round_payout"]

            if end["phase"] == "base":
                # Вариант Y: лестница не касается базового раунда вовсе.
                assert end["multiplier_applied"] == 1
                assert end["ladder"] is None
                assert end["scatter_count"] == result["scatter_count"]
                assert end["freespins_awarded"] == result["freespins_awarded"]
            else:
                assert end["ladder"] is not None
                assert end["scatter_count"] is None, "фриспин-раунд Тето не переначисляет скаттером"
                assert end["freespins_awarded"] is None

        assert total == result["total_payout"], f"seed={seed}: {total} != {result['total_payout']}"


def test_trace_tumble_step_numbering_is_round_global_not_per_cascade():
    """200 сидов. `max(tumble.step)` по раунду == `round_end.tumble_steps_used`,
    а сами `step` внутри раунда идут 1..N без пропусков и повторов.

    Регрессия на конкретный дефект старого трейса: `step` считался ЛОКАЛЬНО
    внутри каждого вызова каскада, поэтому во втором каскаде раунда (после
    волны Дрель-Ханта) НАЧИНАЛСЯ ЗАНОВО с 1, а сама волна не нумеровалась
    вообще — упорядочить шаги раунда или сверить их число с
    `tumble_steps_used` из трейса было невозможно."""
    saw_multi_cascade_round = False
    for seed in range(200):
        trace: list = []
        play_one_spin(random.Random(seed), bet_per_line=1, trace=trace)

        for round_ops in _group_rounds(trace):
            end = round_ops[-1]
            steps = [op["step"] for op in round_ops if op["op"] == "tumble"]
            assert steps == list(range(1, len(steps) + 1)), (
                f"seed={seed}, раунд {end['round']}: нумерация шагов {steps} не сквозная 1..N"
            )
            assert len(steps) == end["tumble_steps_used"], (
                f"seed={seed}, раунд {end['round']}: шагов в трейсе {len(steps)}, "
                f"tumble_steps_used={end['tumble_steps_used']}"
            )
            sources = {op["source"] for op in round_ops if op["op"] == "tumble"}
            if len(steps) >= 2 and "drill_hunt_wave" in sources:
                # Именно этот раунд (волна + хотя бы один обычный шаг) старая
                # нумерация и ломала — фиксируем, что такой случай реально
                # встретился в выборке, а не проверен вхолостую.
                saw_multi_cascade_round = True

    assert saw_multi_cascade_round, (
        "в выборке не встретилось ни одного раунда с волной Дрель-Ханта И обычным "
        "тумбл-шагом — тест сквозной нумерации не был реально проверен"
    )


def test_trace_drill_hunt_frame_is_pre_wave_and_keeps_blocks_the_wave_removes(monkeypatch):
    """ПОЛНОСТЬЮ форсированный сценарий (никакого сида): подменяем начальное
    заполнение раунда на фикстуру `_make_win_after_injection_grid` и заставляем
    `rng.choice(eligible_ids)` выбрать блок 12 — т.е. Дрель-Хант гарантированно
    порождает волну, снимающую блоки 12,13,14,15.

    Доказываем регрессию на дефект: op `drill_hunt` несёт доску
    ПОСЛЕ-ИНЪЕКЦИИ/ДО-ВОЛНЫ. На ней (а) блок `wild_block_id` уже золотая дрель,
    (б) ВСЕ блоки, которые волна затем снесёт, ещё на месте. Старый трейс нёс
    здесь доску ПОСЛЕ волны, поэтому кадр посадки дрели был невосстановим в
    принципе, а подсветить снесённое волной было нечем — блоков на
    единственном доступном кадре уже не было."""
    monkeypatch.setattr(
        eng, "form_blocks", lambda rng, cells, *, start_id=0: _make_win_after_injection_grid()
    )
    # Один фриспин-раунд: только во фриспинах Дрель-Хант гарантирован
    # (`guaranteed=True`), в базовом раунде наш стаб даёт randint==10 != 1.
    monkeypatch.setattr(eng, "_compute_freespins_awarded", lambda scatter_count: 1)

    trace: list = []
    play_one_spin(_WildOnBlock12Rng(), bet_per_line=1, trace=trace)

    drills = [(i, op) for i, op in enumerate(trace) if op["op"] == "drill_hunt"]
    fired = [(i, op) for i, op in drills if op["wave_fired"]]
    assert fired, "форсированный сценарий обязан дать Дрель-Хант с волной"

    for i, op in fired:
        assert op["fired"] is True
        assert op["wild_block_id"] == 12
        by_id = {b.block_id: b for b in op["blocks"]}
        assert by_id[12].symbol_id == WILD_ID, "на кадре drill_hunt блок уже обязан быть Wild"
        # Геометрия и id блока при конверсии не меняются — фронт анимирует
        # посадку дрели на прямоугольник, который уже нарисован.
        assert (by_id[12].row, by_id[12].col, by_id[12].height, by_id[12].width) == (2, 0, 1, 1)

        wave_evaluate = trace[i + 1]
        wave_tumble = trace[i + 2]
        assert wave_evaluate["op"] == "evaluate"
        assert wave_evaluate["source"] == "drill_hunt_wave"
        assert wave_evaluate["winning_block_ids"] == [12, 13, 14, 15]
        assert wave_evaluate["cells_won"] == 4
        assert wave_tumble["op"] == "tumble"
        assert wave_tumble["source"] == "drill_hunt_wave"
        assert wave_tumble["removed_block_ids"] == [12, 13, 14, 15]

        # Ключевое: ВСЁ, что волна снесла, присутствует на кадре drill_hunt.
        assert set(wave_tumble["removed_block_ids"]) <= set(by_id), (
            "кадр drill_hunt обязан быть ДО волны — иначе снесённые блоки на нём отсутствуют"
        )
        # ...и уже отсутствует на кадре самого tumble.
        assert not (set(wave_tumble["removed_block_ids"]) & {b.block_id for b in wave_tumble["blocks"]})

    # Кадр drill_hunt, когда Дрель-Хант НЕ сработал (базовый раунд этого
    # сценария): фиксированная форма — None/0/False, доска не изменена.
    not_fired = [op for _i, op in drills if not op["fired"]]
    assert not_fired, "ожидался базовый раунд без Дрель-Ханта (randint==10 != 1)"
    for op in not_fired:
        assert op["wild_block_id"] is None
        assert op["cells_converted"] == 0
        assert op["wave_fired"] is False


def test_trace_winning_lines_length_counts_columns_not_distinct_blocks():
    """Тонкость, на которой ошибается наивный оверлей paylines: `length` — это
    число совпавших СТОЛБЦОВ, а `block_ids` — РАЗЛИЧНЫЕ блоки на них, поэтому
    мегаблок шириной 2 занимает на линии ДВА столбца одним id и
    `len(block_ids) < length`. Фронт, рисующий полилинию по числу id, не
    дотянул бы её до конца прогона.

    Форсированная доска: полосная «тихая» база (см. `_make_grid`) + блок 1x2
    (ширина 2) на средней горизонтали `TETO_PAYLINES[2]` в столбцах 0-1 +
    одиночные того же символа в столбцах 2-3 => прогон длиной 4 столбца, но
    всего 3 различных блока. Полосы глушат остальные 49 линий — что выиграла
    РОВНО одна линия, пиновано `len(lines) == 1` (на однородной базе прежнего
    образца это было бы неправдой: каждая клетка лежит хотя бы на горизонтали
    своей строки)."""
    from bot.services.teto_megablock import GRID_SIZE
    from bot.services.teto_megablock import MegaBlock
    from bot.services.teto_tumble import describe_winning_lines

    wide = MegaBlock(block_id=100, symbol_id="teto_chimera", row=2, col=0, height=1, width=2)
    covered = set(wide.cells)
    overrides = {(2, 2): "teto_chimera", (2, 3): "teto_chimera"}
    blocks = [wide] + [
        MegaBlock(
            block_id=r * GRID_SIZE + c,
            symbol_id=overrides.get((r, c), _STRIPE_SYMBOLS[c % 3]),
            row=r, col=c, height=1, width=1,
        )
        for r in range(GRID_SIZE)
        for c in range(GRID_SIZE)
        if (r, c) not in covered
    ]
    assert_valid_partition(blocks)

    lines = describe_winning_lines(blocks)
    assert len(lines) == 1, f"ожидалась ровно одна выигравшая линия (средняя горизонталь), получено {lines}"
    line = lines[0]
    assert line["line_index"] == 2, "средняя горизонталь [2,2,2,2,2,2] — индекс 2 в полном наборе 50"

    assert line["symbol_id"] == "teto_chimera"
    assert line["length"] == 4, "прогон — 4 СТОЛБЦА (широкий блок закрывает два)"
    assert line["block_ids"] == [100, 2 * GRID_SIZE + 2, 2 * GRID_SIZE + 3]
    assert len(line["block_ids"]) == 3
    assert len(line["block_ids"]) < line["length"], (
        "ключевое: различных блоков МЕНЬШЕ, чем совпавших столбцов"
    )
    # Геометрия самодостаточна — фронту не нужна копия TETO_PAYLINES.
    assert line["cells"] == [[2, 0], [2, 1], [2, 2], [2, 3]]

    # Тот же перекос обязан встречаться и в НАСТОЯЩИХ трейсах (мегаблоки в
    # заполнении — обычный игровой путь, а не только форсированная фикстура).
    saw_wide_block_on_a_line = False
    for seed in range(200):
        trace: list = []
        play_one_spin(random.Random(seed), bet_per_line=1, trace=trace)
        for op in trace:
            if op["op"] != "evaluate":
                continue
            for real_line in op["winning_lines"]:
                assert len(real_line["block_ids"]) <= real_line["length"]
                assert len(real_line["cells"]) == real_line["length"]
                assert 3 <= real_line["length"] <= 6
                if len(real_line["block_ids"]) < real_line["length"]:
                    saw_wide_block_on_a_line = True
    assert saw_wide_block_on_a_line, "в 200 реальных спинах не встретилось ни одной линии с мегаблоком"


def test_trace_size_constants_are_the_measured_values_not_whatever_is_in_the_file():
    """Пин конкретных ЧИСЕЛ обоих пределов.

    Зачем отдельный тест на две константы: все остальные проверки усечения
    сравнивают результат С САМОЙ КОНСТАНТОЙ (`rounds_recorded <=
    eng.TRACE_MAX_ROUNDS`), поэтому `TRACE_MAX_ROUNDS = 10**9` удовлетворяет их
    ВСЕ и при этом полностью выключает предел времени показа — тот самый отказ
    ("25 дешёвых фриспин-раундов проигрываются на экране минутами"), ради
    которого он и существует. Пин делает молчаливое отключение невозможным:
    поменять значение можно, но только вместе с этим тестом, т.е. осознанно.

    Оба числа — не "красивые", а ИЗМЕРЕННЫЕ на 20 000 настоящих спинов
    (`random.Random(0..19999)`, полное распределение — в комментариях к самим
    константам): ops — медиана 5, p95 42, p99 54, p99.9 79, максимум 101;
    раунды — медиана 1, p95 8, p99 10, p99.9 15, максимум 19. Ни один из
    20 000 спинов не упёрся ни в один предел. Менять значения можно только
    после такого же перемера, иначе честный путь деградации начнёт срабатывать
    на обычной игре."""
    assert eng.TRACE_MAX_OPS == 120
    assert eng.TRACE_MAX_ROUNDS == 20


def test_serialize_animation_caps_pathological_spin_and_reports_truncation_honestly(monkeypatch):
    """Тот же патологический форс, что и
    `test_tumble_hard_cap_and_freespins_hard_cap_both_respected_simultaneously`
    (пустой `_ForcedRng()` + `_compute_freespins_awarded -> 1000`): 101 раунд
    по 30 тумбл-шагов, ~6.3k op'ов, ~21+ МБ JSON, если отдать как есть.

    Доказываем, что предел РЕАЛЬНО срабатывает и рапортует честно: конверт
    усечён, причина названа, `ops_recorded <= TRACE_MAX_OPS`, КАЖДЫЙ раунд <=
    `complete_through_round` присутствует ЦЕЛИКОМ (частичных раундов не
    бывает — иначе фронт нарисовал бы недоигранный каскад как финальную доску
    раунда с неправильной суммой на экране), а итоговый JSON меньше 512 КиБ.
    Заодно фиксируем, что сырой (внутренний) трейс НЕ усечён — предел живёт на
    границе движок<->транспорт, чтобы 500-сидовая проверка инварианта партиции
    выше не потеряла ни одного промежуточного кадра.

    ПОЧЕМУ ЗДЕСЬ ДВА КОНВЕРТА. При дефолтном `TRACE_MAX_OPS=120` в этот спин
    влезает РОВНО ОДИН раунд (63 op'а на раунд, 2x63 > 120) — а на одном
    раунде все проверки "усечение режет ЦЕЛЫМИ раундами", "раунды идут с 0 без
    пропусков" и "`complete_through_round` == последний оставленный" истинны
    тривиально и не поймали бы ни одну реальную ошибку группировки. Поэтому те
    же инварианты проверяются ВТОРОЙ РАЗ на конверте с явным `max_ops=200`
    (влезает 3 раунда) — единственный способ сделать их непустыми, не ломая
    измеренный дефолт."""
    monkeypatch.setattr(eng, "_compute_freespins_awarded", lambda scatter_count: 1000)

    trace: list = []
    result = play_one_spin(_ForcedRng(), bet_per_line=1, trace=trace)
    paid = result["total_payout"]

    rounds_played = 1 + result["freespins_played"]
    assert rounds_played == 1 + FREESPINS_HARD_CAP
    assert len(_group_rounds(trace)) == rounds_played, "сырой трейс обязан быть НЕ усечён"

    envelope = eng.serialize_animation(trace, paid_total=paid)

    assert envelope["version"] == eng.TRACE_SCHEMA_VERSION
    assert envelope["truncated"] is True
    assert envelope["truncated_reason"] == "op_cap"
    assert envelope["ops_total"] == len(trace)
    assert envelope["rounds_total"] == rounds_played
    assert envelope["ops_recorded"] <= eng.TRACE_MAX_OPS
    assert envelope["ops_recorded"] == len(envelope["ops"])
    assert 1 <= envelope["rounds_recorded"] <= eng.TRACE_MAX_ROUNDS
    assert isinstance(envelope["complete_through_round"], int)
    # Усечение — свойство ПОКАЗА, а не денег: движковый итог считается по всему
    # сырому трейсу, иначе экран показал бы "выплачено X из огрызка".
    assert envelope["payout_engine_total"] == paid == result["total_payout"]

    # (1) дефолтный предел — здесь помещается ровно один раунд.
    _assert_truncation_keeps_whole_contiguous_rounds(envelope)
    assert envelope["rounds_recorded"] == 1, (
        "63 op'а на раунд при пределе 120 — дефолт обязан оставить ровно один раунд; "
        "если это изменилось, проверки ниже (max_ops=200) надо перекалибровать"
    )

    # (2) тот же трейс с поднятым пределом — те же инварианты, но уже на
    # НЕСКОЛЬКИХ раундах (см. докстринг: на одном они вырождены).
    multi = eng.serialize_animation(trace, paid_total=paid, max_ops=200)
    assert multi["truncated"] is True
    assert multi["truncated_reason"] == "op_cap"
    assert multi["rounds_recorded"] == 3, "3 раунда x 63 op'а = 189 <= 200 < 252"
    assert multi["ops_recorded"] == 189
    assert multi["complete_through_round"] == 2
    _assert_truncation_keeps_whole_contiguous_rounds(multi)

    payload = json.dumps(envelope)
    # 512 КиБ — потолок этого КОНКРЕТНОГО конверта (реально ~244 КиБ), не
    # теоретический максимум схемы: тот, по измерению худшего op'а в 4 631 Б,
    # равен 120 x 4 631 = 543 КиБ (см. комментарий к TRACE_MAX_OPS).
    assert len(payload.encode()) < 512 * 1024, f"конверт {len(payload.encode())} Б — предел не держит байты"
    # Доски внутри op'ов — уже plain-dict'ы той же формы, что outcome.final_blocks.
    assert json.loads(payload) == envelope
    for op in envelope["ops"]:
        for block in op["blocks"]:
            assert set(block) == {"block_id", "symbol_id", "row", "col", "height", "width"}


def _assert_truncation_keeps_whole_contiguous_rounds(envelope: dict) -> None:
    """Общий блок проверок усечения (вынесен, потому что применяется к трём
    разным конвертам — двум op_cap'ным и round_cap'ному): оставлены ТОЛЬКО
    целые раунды, начиная с нулевого, без пропусков, и
    `complete_through_round` == номеру последнего оставленного."""
    kept_rounds = _group_rounds(envelope["ops"])
    assert len(kept_rounds) == envelope["rounds_recorded"]
    assert kept_rounds[-1][0]["round"] == envelope["complete_through_round"]
    for round_ops in kept_rounds:
        assert round_ops[0]["op"] == "fill", "усечение обязано резать ЦЕЛЫМИ раундами"
        assert round_ops[-1]["op"] == "round_end"
    # Раунды идут с начала спина и без пропусков — фронт проигрывает
    # 0..complete_through_round, потом прыгает в финал по outcome.
    assert [ops[0]["round"] for ops in kept_rounds] == list(range(envelope["rounds_recorded"]))


def test_serialize_animation_round_cap_truncates_by_time_budget_not_only_by_ops():
    """Предел РАУНДОВ (`TRACE_MAX_ROUNDS`) — единственный тест, который его
    вообще достигает: `truncated_reason == "round_cap"`.

    Почему без него ветка была мертва и почему это не косметика. Предел ops
    бьёт по БАЙТАМ, предел раундов — по ВРЕМЕНИ ПОКАЗА, и ни один не мажорирует
    другой (см. докстринг `serialize_animation`): спин из 25 ДЕШЁВЫХ
    фриспин-раундов по 4 op'а — это всего 100 ops (предел ops молчит), но
    полторы-три секунды на раунд = около минуты анимации, которую никто не
    смотрит. Убери ветку `round_cap` — и весь остальной набор остаётся
    зелёным (проверено мутацией: замена ветки на `pass` не роняла ничего),
    потому что единственный существовавший тест усечения форсирует ДОРОГИЕ
    раунды и всегда упирается в op_cap первым.

    Сид 6178 даёт 26 раундов на настоящем RNG (прежний сид 177 подбирался до
    перекалибровки от 2026-08-06 — разворот `count_scatter_blocks` на сумму
    площадей + новые `WEIGHTS`/`FREESPIN_SCATTER_MIN` уронили частоту
    триггера фриспинов с ~1 к 80-100 до ~1 к 420-450, и сид 177 больше не
    даёт многораундовый спин — новый найден программным перебором сидов
    0..40000 по `freespins_played >= 4` (found 81 таких кандидатов), первый с
    максимальным числом раундов среди найденных); `max_ops` поднят заведомо
    выше всего трейса, чтобы причиной усечения гарантированно был именно
    предел раундов, а не совпадение с байтовым."""
    trace: list = []
    result = play_one_spin(random.Random(6178), bet_per_line=3, trace=trace)

    rounds_played = 1 + result["freespins_played"]
    assert rounds_played == 26, "сид 6178 подобран как многораундовый — если это изменилось, подобрать новый"

    envelope = eng.serialize_animation(
        trace, paid_total=result["total_payout"], max_rounds=3, max_ops=10**6
    )

    assert envelope["truncated"] is True
    assert envelope["truncated_reason"] == "round_cap", (
        "усечение обязано быть по предела РАУНДОВ — max_ops заведомо недостижим"
    )
    assert envelope["rounds_recorded"] == 3
    assert envelope["rounds_total"] == rounds_played
    assert envelope["complete_through_round"] == 2
    assert envelope["ops_recorded"] < envelope["ops_total"], "часть op'ов обязана быть срезана"
    _assert_truncation_keeps_whole_contiguous_rounds(envelope)

    # Граница ровно на числе раундов спина — усечения быть НЕ должно (проверяем
    # `>=`, а не `>`: предел "не более N раундов", а не "менее N").
    exact = eng.serialize_animation(
        trace, paid_total=result["total_payout"], max_rounds=rounds_played, max_ops=10**6
    )
    assert exact["truncated"] is False
    assert exact["rounds_recorded"] == rounds_played


def test_serialize_animation_does_not_truncate_ordinary_spins():
    """Обратная сторона предыдущего теста: на 300 НАСТОЯЩИХ (не форсированных)
    спинах предел не срабатывает ни разу — `truncated is False`,
    `ops_total == ops_recorded`, `rounds_total == rounds_recorded`,
    `complete_through_round` == номеру последнего раунда. Если бы значения
    120/20 были подобраны слишком тесно, честный путь деградации срабатывал бы
    на обычной игре — а это уже видимая игроку потеря анимации, ради
    предотвращения которой пределы и мерились на 20 000 спинов."""
    for seed in range(300):
        trace: list = []
        result = play_one_spin(random.Random(seed), bet_per_line=1, trace=trace)
        envelope = eng.serialize_animation(trace, paid_total=result["total_payout"])

        assert envelope["truncated"] is False, f"seed={seed}: обычный спин усечён"
        assert envelope["truncated_reason"] is None
        assert envelope["ops_total"] == envelope["ops_recorded"] == len(trace)
        assert envelope["rounds_total"] == envelope["rounds_recorded"] == 1 + result["freespins_played"]
        assert envelope["complete_through_round"] == result["freespins_played"]
        assert envelope["ops"][0]["op"] == "fill"
        assert envelope["ops"][-1]["op"] == "round_end"


# ---------------------------------------------------------------------------
# 9. Sampled RTP — грубая CI-полоса (зеркало прецедента Azumanga)
# ---------------------------------------------------------------------------


def test_sampled_rtp_in_band():
    """Грубая CI-полоса RTP — зеркало `tests/test_slot_engine.py::
    test_sampled_rtp_in_band` (Azumanga). Полоса НАМЕРЕННО широкая
    (0.80..1.05 при откалиброванной цели 0.96): этот тест существует, чтобы
    поймать катастрофу класса D-06 (сломанный RTP ~38% на непроверенных
    черновых цифрах паутейбла — прецедент уже был), а НЕ чтобы перепроверять
    калибровку; точная верификация (100k+ спинов, полная статистика: hit
    rate, частота фриспинов, вклад базы/бонуса, хвосты) живёт в
    `scripts/calibrate_teto_rtp.py` и гоняется руками при каждой правке
    `TETO_PAYTABLE`/`WEIGHTS`/`SHAPE_POOL`/`TETO_PAYLINES`. 8 000 спинов —
    ~5 с CI-времени (~1.6k спин/с) и достаточно, чтобы 38% не пролез никогда,
    а честные 96% не флапали: разброс на 8k у этого движка укладывается в
    полосу с большим запасом (замерено на нескольких сидах: 0.95..0.98)."""
    rng = random.Random(12345)
    n_spins = 8_000
    bet_per_line = 10
    total_bet = 0
    total_payout = 0
    for _ in range(n_spins):
        result = play_one_spin(rng, bet_per_line=bet_per_line)
        total_bet += bet_per_line * eng.TOTAL_LINES
        total_payout += result["total_payout"]

    rtp = total_payout / total_bet
    assert 0.80 <= rtp <= 1.05, f"RTP {rtp} вне полосы — калибровка сломана (класс D-06)"
