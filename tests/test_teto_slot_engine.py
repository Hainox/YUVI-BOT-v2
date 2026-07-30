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
    """`freespins = scatter_count if scatter_count >= 5 else 0` — буквально.
    НЕ 3-кейсовый lookup вроде `slot_engine._freespins_for` (тот класс бага
    уже найден для Azumanga — здесь не повторяем, см. `teto_slot_engine.py`)."""
    assert _compute_freespins_awarded(0) == 0
    assert _compute_freespins_awarded(4) == 0
    assert _compute_freespins_awarded(FREESPIN_SCATTER_MIN) == FREESPIN_SCATTER_MIN
    assert _compute_freespins_awarded(6) == 6
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
    """ОДИН полностью детерминированный сценарий (форс-сид 76972, найден
    перебором на прототипе), где заведомо происходит: мегаблок в первичном
    заполнении + хотя бы один тумбл-шаг + Дрель-Хант в базовой игре +
    начисление фриспинов + Дрель-Хант с волной хотя бы в одном фриспин-раунде
    + пересечение хотя бы одного порога лестницы."""
    seed = 76972
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
