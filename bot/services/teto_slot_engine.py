"""Оркестрация полного спина слота "Тето Брейнрот: Дрель-Хант" (roadmap.md,
концепт 6) — параллельный движок к `slot_engine.py` (Azumanga), НЕ его
параметризация: сетка 6x6, мегаблоки (`teto_megablock.py`), тумбл-каскад
(`teto_tumble.py`), Дрель-Хант + лестница множителя (`teto_drillhunt.py`) —
примитивов этого класса в `slot_engine.py` нет вообще ни в каком виде.

`play_one_spin(rng, bet_per_line)` — ЧИСТАЯ функция `(rng, bet_per_line) ->
dict`, без скрытого состояния: rng — ВСЕГДА явный параметр вызывающей стороны
(в проде — тот же `casino_service._rng` seam, что и у Azumanga), включая
фриспин-цикл внутри неё же — в отличие от `slot_engine.evaluate_grid`, который
доигрывает фриспины через СОБСТВЕННЫЙ module-level `_rng` (там это безопасно и
осознанно, проверено RNG-аудитом отдельно; здесь тот же паттерн реинтродуцировал
бы дыру, которую весь RNG-аудит существует, чтобы закрыть — см. roadmap.md).

Пока — ЧИСТЫЙ движок без интеграции с БД/`casino_service`/деньгами (это
следующий шаг, `play_teto_slots` по образцу `play_slots`, см. roadmap.md)."""

from __future__ import annotations

from typing import Optional

from bot.services.teto_drillhunt import DrillHuntOutcome
from bot.services.teto_drillhunt import LadderState
from bot.services.teto_drillhunt import apply_drill_hunt
from bot.services.teto_drillhunt import apply_ladder
from bot.services.teto_megablock import ALL_CELLS
from bot.services.teto_megablock import MegaBlock
from bot.services.teto_megablock import assert_valid_partition
from bot.services.teto_megablock import form_blocks
from bot.services.teto_tumble import count_scatter_blocks
from bot.services.teto_tumble import evaluate_paylines
from bot.services.teto_tumble import resolve_tumble

# Хардкап на число тумбл-шагов ЗА ОДИН РАУНД (базовый или один фриспин-раунд)
# — аналог `slot_engine.FREESPINS_HARD_CAP`, но на цепочку каскадов, а не на
# число бонусных спинов: патологический (сколь угодно маловероятный) RNG-стрик
# без него мог бы каскадить неограниченно. Волна Дрель-Ханта (см.
# `teto_drillhunt.apply_drill_hunt`) засчитывается в этот же общий бюджет.
TUMBLE_HARD_CAP = 30

# Хардкап на суммарное число фриспин-раундов за один спин (изначальная выдача
# + все ретриггеры вместе) — та же роль, что `slot_engine.FREESPINS_HARD_CAP`.
FREESPINS_HARD_CAP = 100

FREESPIN_SCATTER_MIN = 5


def compute_freespins_awarded(scatter_count: int) -> int:
    """`freespins = scatter_count if scatter_count >= 5 else 0` — буквально
    из roadmap.md, ОТКРЫТАЯ формула. Намеренно НЕ 3-кейсовый lookup вроде
    `slot_engine._freespins_for` (тот класс бага уже найден и задокументирован
    в roadmap.md для Azumanga — здесь не повторяем: скаттер-мегаблок может
    закрыть 6-16 клеток одним блоком, поэтому подстановка фиксированного
    числа фриспинов независимо от `scatter_count>=5` занижала бы выплату)."""
    return scatter_count if scatter_count >= FREESPIN_SCATTER_MIN else 0


def run_tumble_cascade(
    rng, blocks: list[MegaBlock], hard_cap: int, *, trace: Optional[list] = None
) -> tuple[list[MegaBlock], int, int]:
    """Повторяет `evaluate_paylines` -> `resolve_tumble`, пока есть выигрыш и
    `hard_cap` не исчерпан. Возвращает `(blocks, total_winning_cells,
    шагов_использовано)`."""
    steps = 0
    total_cells = 0
    while steps < hard_cap:
        has_win, winning_ids = evaluate_paylines(blocks)
        if trace is not None:
            trace.append({"op": "evaluate_paylines", "has_win": has_win, "blocks": list(blocks)})
        if not has_win:
            break
        total_cells += sum(b.area for b in blocks if b.block_id in winning_ids)
        blocks = resolve_tumble(rng, blocks, winning_ids)
        steps += 1
        if trace is not None:
            trace.append({"op": "resolve_tumble", "step": steps, "blocks": list(blocks)})
    return blocks, total_cells, steps


def play_one_round(
    rng, blocks: list[MegaBlock], bet_per_line, *,
    guaranteed_drill_hunt: bool, tumble_hard_cap: int, trace: Optional[list] = None,
) -> dict:
    """Один "раунд" = тумбл-каскад до исчерпания выигрышей ИЛИ `hard_cap`,
    затем РОВНО ОДИН вызов Дрель-Ханта (чья единственная опциональная волна
    засчитывается в тот же общий `hard_cap`), затем — если волна сработала —
    возврат во внешний тумбл-цикл: если она оставила на доске ещё один
    выигрыш, он доигрывается как обычный тумбл (в пределах оставшегося
    бюджета `hard_cap`)."""
    blocks, cells_won_a, steps_a = run_tumble_cascade(rng, blocks, tumble_hard_cap, trace=trace)

    remaining_cap = tumble_hard_cap - steps_a
    outcome: DrillHuntOutcome = apply_drill_hunt(
        rng, blocks, guaranteed=guaranteed_drill_hunt, tumble_hard_cap_remaining=remaining_cap,
    )
    blocks = outcome.blocks
    if trace is not None:
        trace.append({
            "op": "drill_hunt",
            "cells_converted": outcome.cells_converted,
            "wave_fired": outcome.triggered_new_tumble,
            "blocks": list(blocks),
        })

    steps_b = 0
    cells_won_wave = 0
    if outcome.triggered_new_tumble:
        steps_b = 1  # волна = один тумбл-шаг против общего hard_cap
        cells_won_wave = outcome.wave_cells_won

    remaining_cap2 = tumble_hard_cap - steps_a - steps_b
    blocks, cells_won_c, steps_c = run_tumble_cascade(rng, blocks, max(0, remaining_cap2), trace=trace)

    total_cells_won = cells_won_a + cells_won_wave + cells_won_c
    tumble_steps_used = steps_a + steps_b + steps_c

    return {
        "blocks": blocks,
        "raw_round_payout": bet_per_line * total_cells_won,
        "total_cells_won": total_cells_won,
        "cells_converted_by_drill_hunt": outcome.cells_converted,
        "drill_hunt_fired": outcome.cells_converted > 0,
        "drill_hunt_wave_fired": outcome.triggered_new_tumble,
        "tumble_steps_used": tumble_steps_used,
        "tumble_hard_cap_hit": tumble_steps_used >= tumble_hard_cap,
    }


def play_one_spin(rng, bet_per_line, *, trace: Optional[list] = None) -> dict:
    """Оркестрирует ПОЛНЫЙ спин: заполнение -> тумбл-цикл (`TUMBLE_HARD_CAP`)
    -> Дрель-Хант (не гарантирован) -> если фриспины начислены — цикл
    фриспин-раундов (каждый: заполнение -> тумбл -> Дрель-Хант
    (`guaranteed=True`) -> обновление лестницы), капнутый `FREESPINS_HARD_CAP`.

    Возвращает dict — см. поля ниже. ЧИСТАЯ функция: два вызова с двумя
    одинаково сконфигурированными свежими rng-стабами дают побайтово
    идентичный результат (см. `tests/test_teto_slot_engine.py`)."""
    initial_blocks = form_blocks(rng, set(ALL_CELLS))
    if trace is not None:
        trace.append({"op": "initial_fill", "blocks": list(initial_blocks)})
    assert_valid_partition(initial_blocks)

    base_result = play_one_round(
        rng, initial_blocks, bet_per_line,
        guaranteed_drill_hunt=False, tumble_hard_cap=TUMBLE_HARD_CAP, trace=trace,
    )

    scatter_count = count_scatter_blocks(base_result["blocks"])
    freespins_awarded = compute_freespins_awarded(scatter_count)

    # Лестница живёт ТОЛЬКО во фриспинах (зафиксировано владельцем бота
    # 2026-07-30) — создаётся заново здесь, очки Дрель-Ханта из base_result
    # выше в неё не идут.
    ladder = LadderState()
    fs_round_records: list[dict] = []
    freespins_played = 0
    remaining = freespins_awarded

    while remaining > 0 and freespins_played < FREESPINS_HARD_CAP:
        remaining -= 1
        freespins_played += 1

        fs_blocks = form_blocks(rng, set(ALL_CELLS))
        if trace is not None:
            trace.append({"op": f"fs_round_{freespins_played}_initial_fill", "blocks": list(fs_blocks)})
        assert_valid_partition(fs_blocks)

        fs_result = play_one_round(
            rng, fs_blocks, bet_per_line,
            guaranteed_drill_hunt=True, tumble_hard_cap=TUMBLE_HARD_CAP, trace=trace,
        )

        ladder, extra_fs, final_payout = apply_ladder(
            ladder, fs_result["cells_converted_by_drill_hunt"], fs_result["raw_round_payout"],
        )
        remaining += extra_fs

        fs_round_records.append({
            "round_index": freespins_played,
            "blocks": fs_result["blocks"],
            "raw_round_payout": fs_result["raw_round_payout"],
            "final_round_payout": final_payout,
            "cells_converted_by_drill_hunt": fs_result["cells_converted_by_drill_hunt"],
            "drill_hunt_wave_fired": fs_result["drill_hunt_wave_fired"],
            "tumble_steps_used": fs_result["tumble_steps_used"],
            "tumble_hard_cap_hit": fs_result["tumble_hard_cap_hit"],
            "ladder_multiplier_after": ladder.multiplier,
            "ladder_score_after": ladder.score,
            "extra_freespins_awarded_this_round": extra_fs,
        })

    total_fs_final_payout = sum(r["final_round_payout"] for r in fs_round_records)
    final_blocks = fs_round_records[-1]["blocks"] if fs_round_records else base_result["blocks"]

    return {
        "initial_blocks": initial_blocks,
        "base_round": base_result,
        "scatter_count": scatter_count,
        "freespins_awarded": freespins_awarded,
        "freespins_played": freespins_played,
        "freespins_hard_cap_hit": freespins_played >= FREESPINS_HARD_CAP and remaining > 0,
        "fs_round_records": fs_round_records,
        "ladder_final_state": ladder,
        "total_payout": base_result["raw_round_payout"] + total_fs_final_payout,
        "final_blocks": final_blocks,
    }
