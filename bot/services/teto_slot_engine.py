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

Сам модуль остаётся ЧИСТЫМ (никакой БД/сессий/денег внутри) — money/DB-обвязка
живёт снаружи, в `casino_service.play_teto_slots` (по образцу `play_slots`).

Два разных выхода одного и того же спина, которые НЕЛЬЗЯ путать:
  - `serialize_spin_result(result)` -> в `CasinoGame.outcome` (JSONB) — ЗАПИСЬ
    ФАКТА: финальные доски раундов, выплаты, лестница. Есть всегда, включая
    replay, не усекается никогда.
  - `serialize_animation(trace)` -> в HTTP-ответ (ключ `animation`) —
    ПРЕЗЕНТАЦИОННЫЙ СЦЕНАРИЙ для анимации миниаппа: покадровый лог спина
    (`fill`/`evaluate`/`tumble`/`drill_hunt`/`round_end`). Может отсутствовать
    (replay), может быть усечён по размеру. В `outcome` не попадает НИКОГДА —
    почему именно, подробно в докстринге `serialize_animation`.
Это тот же урок, что уже выучен на Azumanga (`slot_engine.SlotResult.
freespin_rounds`): фронту, получающему только ИТОГОВОЕ число вместо данных на
каждый раунд, нечего показать, и игрок читает результат как «авторасчёт, а не
полный прокрут». Здесь то же самое применено к каскадам внутри раунда."""

from __future__ import annotations

import dataclasses

from bot.services.teto_drillhunt import DrillHuntOutcome
from bot.services.teto_drillhunt import LadderState
from bot.services.teto_drillhunt import apply_drill_hunt
from bot.services.teto_drillhunt import apply_ladder
from bot.services.teto_megablock import ALL_CELLS
from bot.services.teto_megablock import MegaBlock
from bot.services.teto_megablock import assert_valid_partition
from bot.services.teto_megablock import form_blocks
from bot.services.teto_tumble import TETO_PAYLINES
from bot.services.teto_tumble import count_scatter_blocks
from bot.services.teto_tumble import describe_winning_lines
from bot.services.teto_tumble import evaluate_paylines
from bot.services.teto_tumble import resolve_tumble

# Число линий этого слота — тот же паттерн, что `slot_engine.TOTAL_LINES =
# len(slot_data.PAYLINES)`: единственный источник истины — фактическая длина
# `TETO_PAYLINES` (сейчас 3, MVP-подмножество будущих 50, см. докстринг
# `teto_tumble.py`), а не захардкоженное число, которое рассинхронизировалось
# бы при правке набора линий. Используется `casino_service.play_teto_slots`
# для валидации `bet % TOTAL_LINES == 0` (по образцу `play_slots`).
TOTAL_LINES = len(TETO_PAYLINES)

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

# --- Трейс анимации: версия схемы и два независимых предела размера ----------

# Версия схемы трейса (ключ `version` в конверте `serialize_animation`). Нужна
# потому, что контракт ГАРАНТИРОВАННО поедет: расширение набора линий 3 -> 50
# уже запланировано (см. `teto_tumble.TETO_PAYLINES`). Без явной версии
# миниапп на старом деплое отрисовал бы новую форму молча-неправильно вместо
# того, чтобы честно деградировать до финальной доски из `outcome`.
TRACE_SCHEMA_VERSION = 1

# Предел на число ОПЕРАЦИЙ в отдаваемом наружу трейсе — бюджет БАЙТ ответа.
# Конкретный риск: худший op ~4.0 КБ (доска из 36 одиночных блоков 3.6 КБ +
# скаляры + до 3 описанных линий), а патологический (форсированный) спин
# 100 фриспин-раундов x 30 тумбл-шагов даёт ~6.3k ops -> ~21 МБ JSON-текста.
# Без предела это уехало бы в HTTP-ответ на одном спине. 120 ops -> <=480 КБ
# в самом худшем случае (~44x сокращение) и при этом НЕ срезает ни один
# реальный спин: на 20 000 настоящих спинов (`random.Random(0..19999)`)
# распределение ops — медиана 5, p95 42, p99 53, p99.9 78, максимум 99, т.е.
# усечение при 120 не наступило НИ РАЗУ (0 из 20 000; при пределе 60 срезало
# бы 0.82%, при 90 — 0.04%). Предел живёт в `serialize_animation`, а НЕ в
# `play_one_spin` — см. её докстринг про 500-сидовый инвариант партиции.
TRACE_MAX_OPS = 120

# Предел на число РАУНДОВ в трейсе — бюджет ВРЕМЕНИ анимации, а не байт: это
# принципиально другой отказ. 20 крошечных раундов по 4 op — всего 80 ops
# (предел ops не сработает), но проиграть их на экране по ~1.5-3 с/раунд —
# уже 30-60 с, которые никто не смотрит; 100 раундов — минуты. Оба предела
# нужны одновременно, потому что ни один из них не мажорирует другой.
# На тех же 20 000 реальных спинов раунды: медиана 1, p95 8, p99 10, p99.9 15,
# максимум 19 — усечения по 20 не было ни разу (при 12 срезало бы 0.79%).
TRACE_MAX_ROUNDS = 20

# Полный словарь op'ов трейса — РОВНО пять, ни один НЕ содержит цифры.
# Держится здесь как единственный источник истины для тестов схемы.
TRACE_OPS = ("fill", "evaluate", "tumble", "drill_hunt", "round_end")


def _compute_freespins_awarded(scatter_count: int) -> int:
    """`freespins = scatter_count if scatter_count >= 5 else 0` — буквально
    из roadmap.md, ОТКРЫТАЯ формула. Намеренно НЕ 3-кейсовый lookup вроде
    `slot_engine._freespins_for` (тот класс бага уже найден и задокументирован
    в roadmap.md для Azumanga — здесь не повторяем: скаттер-мегаблок может
    закрыть 6-16 клеток одним блоком, поэтому подстановка фиксированного
    числа фриспинов независимо от `scatter_count>=5` занижала бы выплату)."""
    return scatter_count if scatter_count >= FREESPIN_SCATTER_MIN else 0


def _record(
    trace: list, op: str, *, phase: str, round_index: int, blocks: list[MegaBlock], **extra
) -> None:
    """Дописывает ОДИН op в трейс анимации. Порядок ключей фиксирован:
    `op`, `seq`, `phase`, `round`, op-специфичные, `blocks` (всегда последним) —
    чтобы дамп трейса в `psql`/логах читался глазами без прокрутки доски.

    ПОЧЕМУ идентичность раунда — структурные `phase`/`round`, а НЕ имя op'а.
    Старый трейс кодировал номер раунда в СТРОКЕ имени
    (`f"fs_round_{n}_initial_fill"`), а тумбл/дрель-op'ы внутри фриспин-раунда
    не несли маркера раунда вообще. Цена для потребителя была не косметическая:
    чтобы просто разбить трейс на раунды, фронту пришлось бы парсить имена
    регуляркой И ДОГАДЫВАТЬСЯ о сегментации остальных op'ов по позиции между
    "fill"-ами; любая правка формата имени (или второй слот с другим
    префиксом) молча ломала бы такой парсер. Здесь: `phase` ∈
    {"base","freespin"}, `round` = 0 для базового раунда и 1..N для
    фриспин-раундов (совпадает с `fs_round_records[round-1]["round_index"]`),
    ни одно имя op'а не содержит цифры вовсе (регрессия — тест схемы).

    ПОЧЕМУ `blocks` есть на КАЖДОМ op'е, включая избыточный `evaluate` (его
    доска равна доске предыдущего op'а). Две причины, обе перевешивают ~2x
    оверхед байт (под который и подобран `TRACE_MAX_OPS`):
      1. `test_partition_invariant_holds_through_entire_spin_including_freespins`
         гоняет `entry["blocks"]` для КАЖДОЙ записи трейса на 500 сидах —
         это единственная проверка инварианта партиции на ПРОМЕЖУТОЧНЫХ
         досках; op без `blocks` тихо вынул бы кадр из-под этой проверки.
      2. Seek-to-any-op: кнопка "пропустить/промотать" в миниаппе прыгает на
         op k и рисует его немедленно, без переигрывания с нуля. Дельты
         (отклонённая альтернатива, см. `serialize_animation`) это ломают.

    Правило фиксированной формы: op НИКОГДА не опускает ключ. Неприменимое
    значение — `None`/`[]`/`0`, но не отсутствие ключа: потребителю не нужно
    писать `if (key in op)`, и опечатка в имени ключа на бэке проявляется как
    `null` в UI, а не как молчаливое падение в ветку "ключа нет"."""
    trace.append({
        "op": op,
        # `seq` дублирует индекс в массиве (усечение сейчас режет только хвост
        # ЦЕЛЫМИ раундами, см. `serialize_animation`), но передаётся явно:
        # дампы грепаются по seq, а гипотетическое будущее выкидывание op'ов из
        # СЕРЕДИНЫ не сможет молча сломать потребителя, который считал индекс.
        "seq": len(trace),
        "phase": phase,
        "round": round_index,
        **extra,
        "blocks": list(blocks),
    })


def _record_evaluate(
    trace: list, *, phase: str, round_index: int, blocks: list[MegaBlock],
    source: str, has_win: bool, winning_ids, bet_per_line: int,
    winning_lines: list[dict] | None = None,
) -> list[int]:
    """Записывает op `evaluate` — кадр ДО удаления, тот самый, на котором
    анимация подсвечивает/взрывает выигравшие блоки. Возвращает отсортированный
    список выигравших id, чтобы вызывающий продублировал его в следующий
    `tumble` (`removed_block_ids`) без второй сортировки.

    Требование "знать, что умрёт, ДО того как оно умрёт" (старый op
    `evaluate_paylines` не говорил, КАКИЕ блоки выиграли, вообще никак)
    выполнено структурно порядком op'ов: `winning_block_ids` этого op'а — ровно
    то, что удалит СЛЕДУЮЩИЙ `tumble`.

    `payout` — СЫРОЙ, ДО множителя лестницы (`bet_per_line * cells_won`), и это
    место, где фронт легко ошибётся: множитель применяется РОВНО РАЗ ЗА РАУНД
    (Вариант Y, см. `teto_drillhunt.apply_ladder`), поэтому умножение на
    каждом шаге дало бы двойное умножение. Инвариант, который стоит держать в
    голове: сумма `evaluate.payout` по раунду == `round_end.raw_round_payout`,
    а множитель виден только в `round_end.multiplier_applied`."""
    winning_list = sorted(winning_ids)
    cells_won = sum(b.area for b in blocks if b.block_id in winning_ids)
    _record(
        trace, "evaluate", phase=phase, round_index=round_index, blocks=blocks,
        source=source,
        has_win=has_win,
        winning_block_ids=winning_list,
        winning_lines=winning_lines if winning_lines is not None else [],
        cells_won=cells_won,
        payout=bet_per_line * cells_won,
    )
    return winning_list


def _run_tumble_cascade(
    rng, blocks: list[MegaBlock], hard_cap: int, bet_per_line: int, *,
    phase: str, round_index: int, step_offset: int = 0, trace: list | None = None,
) -> tuple[list[MegaBlock], int, int]:
    """Повторяет `evaluate_paylines` -> `resolve_tumble`, пока есть выигрыш и
    `hard_cap` не исчерпан. Возвращает `(blocks, total_winning_cells,
    шагов_использовано)`.

    `phase`/`round_index` — обязательные keyword-параметры БЕЗ дефолтов
    (функция приватная, вызывается только из `_play_one_round`): дефолт
    `round_index=0` означал бы, что будущий забывчивый call site молча пишет
    фриспин-раунды в трейс как базовый раунд — ровно тот класс тихой порчи
    сегментации, от которого структурные `phase`/`round` и вводились.

    `step_offset` делает нумерацию `tumble.step` СКВОЗНОЙ ПО РАУНДУ, а не
    локальной для вызова каскада. Старый `step` считался внутри каждого вызова
    и поэтому НАЧИНАЛСЯ ЗАНОВО с 1 во втором каскаде раунда (после волны
    Дрель-Ханта), а сама волна не нумеровалась вообще — из трейса нельзя было
    ни упорядочить шаги, ни сверить их число с `tumble_steps_used`. Теперь
    `max(step)` по раунду == `round_end.tumble_steps_used` (это и проверяет
    отдельный тест)."""
    steps = 0
    total_cells = 0
    while steps < hard_cap:
        has_win, winning_ids = evaluate_paylines(blocks)
        winning_list: list[int] = []
        if trace is not None:
            winning_list = _record_evaluate(
                trace, phase=phase, round_index=round_index, blocks=blocks,
                source="cascade", has_win=has_win, winning_ids=winning_ids,
                bet_per_line=bet_per_line,
                # Проход по линиям только когда выигрыш РЕАЛЬНО есть: на
                # терминальном (has_win=False) evaluate описывать нечего.
                winning_lines=describe_winning_lines(blocks) if has_win else [],
            )
        if not has_win:
            break
        total_cells += sum(b.area for b in blocks if b.block_id in winning_ids)
        blocks = resolve_tumble(rng, blocks, winning_ids)
        steps += 1
        if trace is not None:
            _record(
                trace, "tumble", phase=phase, round_index=round_index, blocks=blocks,
                source="cascade",
                step=step_offset + steps,
                # Дублируем список из предыдущего evaluate намеренно: op должен
                # рисоваться АВТОНОМНО при прыжке seek'ом прямо на него, без
                # оглядки на предыдущий элемент массива.
                removed_block_ids=winning_list,
            )
    return blocks, total_cells, steps


def _play_one_round(
    rng, blocks: list[MegaBlock], bet_per_line, *,
    guaranteed_drill_hunt: bool, tumble_hard_cap: int,
    phase: str, round_index: int, trace: list | None = None,
) -> dict:
    """Один "раунд" = тумбл-каскад до исчерпания выигрышей ИЛИ `hard_cap`,
    затем РОВНО ОДИН вызов Дрель-Ханта (чья единственная опциональная волна
    засчитывается в тот же общий `hard_cap`), затем — если волна сработала —
    возврат во внешний тумбл-цикл: если она оставила на доске ещё один
    выигрыш, он доигрывается как обычный тумбл (в пределах оставшегося
    бюджета `hard_cap`).

    Трейс раунда: `fill` пишет вызывающий (`play_one_spin` — только он знает,
    что раунд начинается), `round_end` — тоже он (только он держит лестницу,
    см. его докстринг); здесь пишутся `evaluate`/`tumble`/`drill_hunt`.
    Волна Дрель-Ханта НАМЕРЕННО не вложенный под-объект, а обычная пара
    `evaluate`+`tumble` с `source="drill_hunt_wave"`: фронт переиспользует
    ТОТ ЖЕ рендерер каскада дословно, а "что выиграла волна" выражено в тех же
    самых ключах, что любой другой выигрыш."""
    blocks, cells_won_a, steps_a = _run_tumble_cascade(
        rng, blocks, tumble_hard_cap, bet_per_line,
        phase=phase, round_index=round_index, step_offset=0, trace=trace,
    )

    remaining_cap = tumble_hard_cap - steps_a
    outcome: DrillHuntOutcome = apply_drill_hunt(
        rng, blocks, guaranteed=guaranteed_drill_hunt, tumble_hard_cap_remaining=remaining_cap,
    )
    # Доска ДО волны (post-injection/pre-wave) — главное содержательное отличие
    # от старого op'а, который нёс доску ПОСЛЕ волны, из-за чего кадр посадки
    # дрели был невосстановим (см. `DrillHuntOutcome`). Она нужна ДВУМ op'ам
    # ниже (`drill_hunt` и `evaluate` волны), поэтому считается один раз здесь.
    # `blocks_after_injection` заполняется `apply_drill_hunt` всегда; fallback на
    # `outcome.blocks` — страховка от шпиона в тестах, собравшего outcome через
    # `_replace` на старом наборе полей.
    pre_wave_blocks = outcome.blocks_after_injection
    if pre_wave_blocks is None:
        pre_wave_blocks = outcome.blocks

    if trace is not None:
        _record(
            trace, "drill_hunt", phase=phase, round_index=round_index, blocks=pre_wave_blocks,
            # `fired` — то же самое выражение, что и `drill_hunt_fired` ниже:
            # один источник истины, чтобы UI и запись раунда не разошлись.
            fired=outcome.cells_converted > 0,
            wild_block_id=outcome.wild_block_id,
            cells_converted=outcome.cells_converted,
            wave_fired=outcome.triggered_new_tumble,
        )

    steps_b = 0
    cells_won_wave = 0
    if outcome.triggered_new_tumble:
        steps_b = 1  # волна = один тумбл-шаг против общего hard_cap
        cells_won_wave = outcome.wave_cells_won
        if trace is not None:
            # Волна НАМЕРЕННО выражена обычной парой `evaluate`+`tumble`
            # (только с `source="drill_hunt_wave"`), а не вложенным
            # под-объектом: фронт переиспользует свой рендерер каскада
            # дословно, а "что выиграла волна" читается из тех же самых ключей,
            # что и любой другой выигрыш.
            wave_winning_list = _record_evaluate(
                trace, phase=phase, round_index=round_index, blocks=pre_wave_blocks,
                source="drill_hunt_wave", has_win=True,
                winning_ids=outcome.wave_winning_block_ids,
                bet_per_line=bet_per_line,
                winning_lines=list(outcome.wave_winning_lines),
            )
            _record(
                trace, "tumble", phase=phase, round_index=round_index, blocks=outcome.blocks,
                source="drill_hunt_wave",
                step=steps_a + steps_b,
                removed_block_ids=wave_winning_list,
            )

    blocks = outcome.blocks

    remaining_cap2 = tumble_hard_cap - steps_a - steps_b
    blocks, cells_won_c, steps_c = _run_tumble_cascade(
        rng, blocks, max(0, remaining_cap2), bet_per_line,
        phase=phase, round_index=round_index, step_offset=steps_a + steps_b, trace=trace,
    )

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


def play_one_spin(rng, bet_per_line, *, trace: list | None = None) -> dict:
    """Оркестрирует ПОЛНЫЙ спин: заполнение -> тумбл-цикл (`TUMBLE_HARD_CAP`)
    -> Дрель-Хант (не гарантирован) -> если фриспины начислены — цикл
    фриспин-раундов (каждый: заполнение -> тумбл -> Дрель-Хант
    (`guaranteed=True`) -> обновление лестницы), капнутый `FREESPINS_HARD_CAP`.

    ЧИСТАЯ функция: два вызова с двумя одинаково сконфигурированными свежими
    rng-стабами дают побайтово идентичный результат (см.
    `tests/test_teto_slot_engine.py`).

    Возвращает dict:
      `initial_blocks`/`base_round` — первичная доска и результат `_play_one_round`
      для базового (не фриспин) раунда.
      `scatter_count`/`freespins_awarded` — по `base_round["blocks"]`.
      `freespins_played` — ИТОГО сыграно фриспин-раундов (изначальная выдача
      + все ретриггеры), может быть меньше `freespins_awarded`, если
      `freespins_hard_cap_hit`.
      `freespins_hard_cap_hit` — True, если `FREESPINS_HARD_CAP` реально
      остановил цикл раньше, чем `remaining` естественно дошёл до 0 (т.е.
      `freespins_played == FREESPINS_HARD_CAP` И к моменту остановки
      оставались ещё неотыгранные фриспины).
      `fs_round_records` — по одной записи на каждый реально сыгранный
      фриспин-раунд (порядок розыгрыша), поля см. в цикле ниже.
      `ladder_final_state` — `LadderState` на конец спина (score=0/multiplier=1,
      если фриспинов не было вовсе — лестница не создавалась).
      `total_payout` — `base_round["raw_round_payout"]` (по множителю лестница
      НЕ трогает базовый раунд) + сумма `final_round_payout` всех фриспин-раундов.
      `final_blocks` — доска последнего сыгранного фриспин-раунда, либо
      `base_round["blocks"]`, если фриспинов не было вовсе.

    `trace` (опциональный список) — ПРЕЗЕНТАЦИОННЫЙ сценарий спина для
    анимации миниаппа: сюда дописывается по op'у на каждый видимый кадр
    (`fill`/`evaluate`/`tumble`/`drill_hunt`/`round_end`, см. `_record`).
    Возвращаемый dict от наличия трейса НЕ зависит ни одним ключом, RNG
    потребляется бит-в-бит так же (всё добавленное — чистые функции), поэтому
    `trace=None` (прод-путь любого вызывающего, которому анимация не нужна) по
    поведению и стоимости идентичен состоянию до появления трейса. Наружу
    трейс уходит ТОЛЬКО через `serialize_animation` — в `CasinoGame.outcome`
    он не попадает никогда (обоснование — там же).

    Op `round_end` пишется ИМЕННО ЗДЕСЬ, а не в `_play_one_round`, по двум
    причинам: (а) только `play_one_spin` держит `LadderState`, а
    `multiplier_applied` обязан быть прочитан ДО `apply_ladder` (Вариант Y:
    множитель раунда — тот, что был активен НА ВХОДЕ, порог, пересечённый
    этим же раундом, начинает действовать только со следующего); (б) закрывающая
    доска раунда до появления `round_end` попадала в трейс НЕ ВСЕГДА — когда
    каскад A выедал весь `TUMBLE_HARD_CAP`, каскад C входил с `hard_cap=0`,
    его `while` не выполнялся ни разу и не записывал ничего, так что потребителю
    приходилось разбирать случаи, какая ветка закрыла раунд. Теперь закрывающая
    доска есть безусловно.

    Op `spin_end` НАМЕРЕННО отсутствует: спиновые агрегаты (`total_payout`,
    `freespins_awarded`/`freespins_played`, `freespins_hard_cap_hit`,
    `ladder_final_state`, `final_blocks`) уже лежат в `outcome`, который есть
    и на свежем спине, и на replay. Дублировать их в конверт анимации значило
    бы создать ВТОРОЙ источник истины для денежных чисел, причём копия
    отсутствовала бы ровно тогда, когда отсутствует анимация. Разделение
    жёсткое: трейс — сценарий показа (может отсутствовать, может быть усечён),
    `outcome` — запись факта (есть всегда, не усекается никогда)."""
    initial_blocks = form_blocks(rng, set(ALL_CELLS))
    if trace is not None:
        _record(trace, "fill", phase="base", round_index=0, blocks=initial_blocks)
    assert_valid_partition(initial_blocks)

    base_result = _play_one_round(
        rng, initial_blocks, bet_per_line,
        guaranteed_drill_hunt=False, tumble_hard_cap=TUMBLE_HARD_CAP,
        phase="base", round_index=0, trace=trace,
    )

    scatter_count = count_scatter_blocks(base_result["blocks"])
    freespins_awarded = _compute_freespins_awarded(scatter_count)

    if trace is not None:
        # Базовый раунд лестницей не умножается вовсе (Вариант Y, зафиксировано
        # владельцем бота 2026-07-30), поэтому `multiplier_applied=1` и
        # `ladder=None` — не "заглушка", а факт: `LadderState` на этот момент
        # ещё даже не создан. Зато только у базового раунда есть скаттер-итог.
        _record(
            trace, "round_end", phase="base", round_index=0, blocks=base_result["blocks"],
            raw_round_payout=base_result["raw_round_payout"],
            multiplier_applied=1,
            final_round_payout=base_result["raw_round_payout"],
            cells_converted_by_drill_hunt=base_result["cells_converted_by_drill_hunt"],
            tumble_steps_used=base_result["tumble_steps_used"],
            tumble_hard_cap_hit=base_result["tumble_hard_cap_hit"],
            scatter_count=scatter_count,
            freespins_awarded=freespins_awarded,
            ladder=None,
        )

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
            _record(
                trace, "fill", phase="freespin", round_index=freespins_played, blocks=fs_blocks
            )
        assert_valid_partition(fs_blocks)

        fs_result = _play_one_round(
            rng, fs_blocks, bet_per_line,
            guaranteed_drill_hunt=True, tumble_hard_cap=TUMBLE_HARD_CAP,
            phase="freespin", round_index=freespins_played, trace=trace,
        )

        # Множитель, применяемый К ЭТОМУ раунду, обязан быть считан ДО
        # `apply_ladder` (Вариант Y — та возвращает УЖЕ обновлённое состояние,
        # чей `multiplier` относится к СЛЕДУЮЩЕМУ раунду). Ровно поэтому
        # `round_end` не может жить внутри `_play_one_round`.
        multiplier_applied = ladder.multiplier

        ladder, extra_fs, final_payout = apply_ladder(
            ladder, fs_result["cells_converted_by_drill_hunt"], fs_result["raw_round_payout"],
        )
        remaining += extra_fs

        if trace is not None:
            # Фриспин-раунд Тето НИКОГДА не переначисляет фриспины скаттером
            # (лишние фриспины приходят только с порогов лестницы), поэтому
            # `scatter_count`/`freespins_awarded` здесь честный `None`, а не 0:
            # 0 читался бы как "скаттеров не выпало", хотя их тут не считают.
            _record(
                trace, "round_end", phase="freespin", round_index=freespins_played,
                blocks=fs_result["blocks"],
                raw_round_payout=fs_result["raw_round_payout"],
                multiplier_applied=multiplier_applied,
                final_round_payout=final_payout,
                cells_converted_by_drill_hunt=fs_result["cells_converted_by_drill_hunt"],
                tumble_steps_used=fs_result["tumble_steps_used"],
                tumble_hard_cap_hit=fs_result["tumble_hard_cap_hit"],
                scatter_count=None,
                freespins_awarded=None,
                ladder={
                    "score_after": ladder.score,
                    "multiplier_after": ladder.multiplier,
                    "crossed_thresholds": sorted(ladder.crossed_thresholds),
                    "extra_freespins_awarded": extra_fs,
                },
            )

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


def _serialize_blocks(blocks: list[MegaBlock]) -> list[dict]:
    """Конвертирует список `MegaBlock` в список plain-dict через
    `dataclasses.asdict` — берёт РОВНО 6 настоящих dataclass-полей
    (`block_id`/`symbol_id`/`row`/`col`/`height`/`width`); `cells`/`area` —
    вычисляемые `@property`, не поля датакласса, поэтому `asdict` их
    автоматически НЕ включает (не нужно вручную вырезать) — итоговый dict уже
    JSON-совместим как есть."""
    return [dataclasses.asdict(b) for b in blocks]


def serialize_spin_result(result: dict) -> dict:
    """Приводит "сырой" (как из `play_one_spin`) результат к форме, которую
    можно безопасно положить в `CasinoGame.outcome` (колонка JSONB) —
    `casino_service.play_teto_slots` кладёт СЮДА возвращаемое значение этой
    функции, а не сырой `result`.

    Зачем это вообще нужно: `play_one_spin` возвращает dict, но НЕ каждое
    значение внутри него — примитив. Три места содержат объекты, которые
    `json.dumps`/asyncpg-JSONB-кодек не умеют сериализовать напрямую:
      1. Списки `MegaBlock` (frozen-датакласс) — на ВЕРХНЕМ уровне
         (`initial_blocks`/`final_blocks`), внутри `base_round["blocks"]` и
         внутри КАЖДОЙ записи `fs_round_records[i]["blocks"]`. Пропуск хотя бы
         одного из этих вложенных мест — не абстрактный риск: `fs_round_records`
         непустой в любом спине, где выпало >=5 скаттер-блоков (обычный игровой
         путь, не экзотика), так что забытая конверсия там уронит commit
         РЕАЛЬНОГО пользовательского спина с ошибкой сериализации, а не только
         синтетический тест.
      2. `ladder_final_state` (`LadderState`) — датакласс с полем
         `crossed_thresholds: set`, а JSON/JSONB множеств не знает вообще;
         конвертируем в отсортированный список int (детерминированный порядок
         для стабильного diff'а при отладке через `psql`).

    Всё остальное (`scatter_count`/`freespins_awarded`/`freespins_played`/
    `freespins_hard_cap_hit`/`total_payout`, скалярные поля внутри
    `base_round`/`fs_round_records`) уже int/bool/str — переносится как есть
    через `**result`, без ручного перечисления каждого поля (при добавлении
    нового скалярного поля в `play_one_spin` в будущем не нужно будет
    вспоминать про эту функцию)."""
    base_round = result["base_round"]
    fs_round_records = result["fs_round_records"]
    ladder = result["ladder_final_state"]

    return {
        **result,
        "initial_blocks": _serialize_blocks(result["initial_blocks"]),
        "base_round": {**base_round, "blocks": _serialize_blocks(base_round["blocks"])},
        "fs_round_records": [
            {**rec, "blocks": _serialize_blocks(rec["blocks"])} for rec in fs_round_records
        ],
        "ladder_final_state": {
            "score": ladder.score,
            "multiplier": ladder.multiplier,
            "crossed_thresholds": sorted(ladder.crossed_thresholds),
        },
        "final_blocks": _serialize_blocks(result["final_blocks"]),
    }


def serialize_animation(
    trace: list[dict],
    *,
    max_ops: int = TRACE_MAX_OPS,
    max_rounds: int = TRACE_MAX_ROUNDS,
) -> dict:
    """Приводит сырой `trace` (как его набил `play_one_spin`) к JSON-совместимому
    конверту анимации И ОГРАНИЧИВАЕТ его размер. Отдельная функция от
    `serialize_spin_result` НАМЕРЕННО: результат той уходит в JSONB-колонку
    `CasinoGame.outcome`, результат этой — только в HTTP-ответ, и две разные
    функции делают "трейс случайно оказался в outcome" структурно невозможным,
    а не соглашением, которое кто-то должен помнить.

    ПОЧЕМУ ТРЕЙС НИКОГДА НЕ ЕДЕТ В `CasinoGame.outcome` — четыре конкретных
    причины, не эстетика:
      1. Раздувание КАЖДОЙ строки: медианы измерены — `outcome` ~10.6 КБ,
         трейс ~17 КБ, т.е. +160% на строку данными с нулевой аудиторской
         ценностью, и записывалось бы это на КАЖДОМ спине, включая ~64%
         спинов вообще без фриспинов (авто-спин в миниаппе — клиентский цикл,
         см. `casino_service._check_slots_throttle`).
      2. Окно блокировки, а не только диск: патологическая строка выросла бы с
         373.5 КБ до ~21 МБ, и этот INSERT происходит ВНУТРИ единственной
         транзакции `_settle` — той же, что держит row-lock на `chat_bank`,
         взятый `economy_service.pay_from_bank`. Сериализовать и записать там
         21 МБ — удлинить блокировку горячей строки чата. Это дефект
         пропускной способности, а не потраченные байты.
      3. Не тот род данных: аудиторская запись, нужная при разборе спора, —
         финальные доски по раундам + выплаты + лестница, и она УЖЕ лежит в
         `outcome`. Сценарий анимации — не доказательство.
      4. Такой JSONB ушёл бы в TOAST out-of-line и распаковывался бы при
         КАЖДОМ обычном чтении строки, включая ручной `SELECT *` в psql.

    ДВА ПРЕДЕЛА, ДВА РАЗНЫХ КОНЦЕРНА (см. константы `TRACE_MAX_OPS` /
    `TRACE_MAX_ROUNDS`): первый бьёт по БАЙТАМ, второй — по ВРЕМЕНИ показа.
    Ни один не мажорирует другой: 20 крошечных раундов по 4 op — 80 ops (предел
    ops молчит), но это уже до минуты анимации; наоборот, один раунд с полным
    каскадом на 30 шагов — 63 op'а в одном раунде (предел раундов молчит).
    Арифметика: худший op ~4.0 КБ (доска 36x1x1 = 3 590 Б измеренных + <=120 Б
    скаляров + <=330 Б описания линий) => 120 ops <= ~480 КБ; типичный спин
    ~17 КБ (5 ops), p95 ~135 КБ (42 ops); НЕусечённый патологический спин
    (`_ForcedRng` + `_compute_freespins_awarded -> 1000`, т.е. ровно форс из
    `test_tumble_hard_cap_and_freespins_hard_cap_both_respected_simultaneously`)
    — 225 432 блок-словаря = 21 678 218 Б ≈ 21 МБ JSON-текста. Т.е. предел
    превращает 21 МБ в <=480 КБ (~44x) и при этом не задевает ни один из
    20 000 реальных спинов (усечение 0 из 20 000 и по ops, и по раундам).

    УСЕЧЕНИЕ — ТОЛЬКО ЦЕЛЫМИ РАУНДАМИ. Раунды в трейсе идут непрерывными
    группами по построению (раунды играются последовательно), группируем по
    `(phase, round)` и берём раунд целиком, только если ОБА бюджета вмещают его
    полностью. Частичный раунд не отдаётся никогда: потребитель, получивший
    раунд, обрезанный посередине каскада, нарисовал бы недоигранный каскад как
    ФИНАЛЬНУЮ доску этого раунда с неправильной суммой на экране — ровно тот
    отказ ("показать частичный спин как полный"), ради предотвращения которого
    предел и существует.

    ⚠ `complete_through_round` — САМАЯ ВЕРОЯТНАЯ ошибка интеграции во всём
    контракте: базовый раунд имеет номер `0`, а `0` в JavaScript ЛОЖЕН.
    Потребитель обязан проверять `complete_through_round !== null`, НИКОГДА не
    `if (complete_through_round)` — иначе спин, у которого поместился ровно
    базовый раунд, будет обработан как "не поместилось ничего". `null` здесь
    означает "не влез даже базовый раунд" (сегодня структурно недостижимо —
    <=65 ops на раунд < 120 — но значение определено всегда).

    Контракт деградации при `truncated`: проиграть раунды
    `0..complete_through_round` из `ops`, затем прыгнуть в финальное состояние
    по `outcome` и честно показать индикатор (`rounds_total - rounds_recorded`
    раундов пропущено). Никогда не выдавать усечённый трейс за весь спин.

    Контракт "анимации может не быть вообще": `animation === null` в ответе
    роута => НЕ анимировать; рисовать финальную доску из `outcome.final_blocks`,
    лестницу из `outcome.ladder_final_state`, деньги из `payout`/
    `user_balance_after`. Так бывает на идемпотентном replay уже
    рассчитанного `idem_key` (см. `casino_service.play_teto_slots`) — и это же
    ПРАВИЛЬНОЕ продуктовое поведение: игрок этот спин уже посмотрел, replay —
    это сетевой ретрай, а не новый раунд.

    ОТКЛОНЁННЫЕ альтернативы сокращения размера (чтобы их не переоткрывали):
      - Дельты вместо полных снапшотов. Отклонено: чтобы восстановить шаг на
        клиенте, пришлось бы переписать на TypeScript и порядок rigid-body
        гравитации (`sorted(survivors, key=lambda b: -(b.row + b.height))`), и
        аллокацию id при доборе (`max(все id) + 1`, включая уже удалённых
        победителей) — дублирование денежно-смежной логики движка, т.е. ровно
        тот класс расхождения, который комментарий в `resolve_tumble`
        фиксирует как УЖЕ однажды поймавшийся на прототипе. Плюс это убивает
        seek-to-any-op.
      - Компактная позиционная кодировка блока `[block_id, symbol_id, row,
        col, height, width]` — ~30 Б против 97 Б, честный ~3x выигрыш, но она
        вводит ВТОРУЮ кодировку доски в тот же ответ (`outcome.final_blocks`
        остаётся многословной через `_serialize_blocks`), заставляя фронт
        держать два парсера одного понятия. Предел ops уже даёт нужный запас;
        одна кодировка везде дороже 3x.
      - Убрать `blocks` из `evaluate`-op'ов (~50% пейлоада). Отклонено дважды —
        см. `_record`."""
    # Группировка по (phase, round) с сохранением порядка: раунды непрерывны по
    # построению, поэтому достаточно сравнивать с последней группой — dict/
    # sort не нужны, и порядок раундов гарантированно совпадает с порядком
    # розыгрыша (важно: усечение обязано срезать ПОСЛЕДНИЕ раунды, не любые).
    groups: list[list[dict]] = []
    last_key: tuple[str, int] | None = None
    for op in trace or ():
        key = (op["phase"], op["round"])
        if key != last_key:
            groups.append([])
            last_key = key
        groups[-1].append(op)

    kept: list[dict] = []
    rounds_recorded = 0
    truncated_reason: str | None = None
    for round_ops in groups:
        if rounds_recorded >= max_rounds:
            truncated_reason = "round_cap"
            break
        if len(kept) + len(round_ops) > max_ops:
            truncated_reason = "op_cap"
            break
        kept.extend(round_ops)
        rounds_recorded += 1

    # `_serialize_blocks` переиспользуется целиком: доска в трейсе побайтово
    # той же формы, что `outcome.final_blocks` — ОДНА кодировка доски на весь
    # ответ, фронт парсит одну структуру и там, и там. `blocks` уже последний
    # ключ, поэтому распаковка через `**op` порядок ключей не ломает.
    ops_out = [{**op, "blocks": _serialize_blocks(op["blocks"])} for op in kept]

    return {
        "version": TRACE_SCHEMA_VERSION,
        "ops": ops_out,
        "ops_recorded": len(ops_out),
        "ops_total": len(trace or ()),
        "rounds_recorded": rounds_recorded,
        "rounds_total": len(groups),
        # Наибольший ПОЛНОСТЬЮ поместившийся раунд. Раунды идут по возрастанию
        # (база=0, затем фриспины 1..N), усечение режет только хвост — значит
        # это просто `round` последнего оставленного op'а. См. предупреждение
        # про falsy-0 в JS выше.
        "complete_through_round": kept[-1]["round"] if kept else None,
        "truncated": truncated_reason is not None,
        "truncated_reason": truncated_reason,
    }
