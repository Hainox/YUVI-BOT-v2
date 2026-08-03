"""Генератор РЕАЛЬНЫХ фикстур анимации слота "Тето Брейнрот: Дрель-Хант" для
dev-моков и визуального QA миниаппа.

Зачем это существует: экран слота и Playwright-ревью мокают POST
/api/v1/games/teto_slots, и мок обязан быть НАСТОЯЩИМ ответом движка, а не
JSON, написанным руками по памяти о контракте — рукописная фикстура молча
разъезжается с формой `serialize_animation`/`serialize_spin_result` при первой
же правке трейса (тот же класс дрейфа, от которого `TRACE_SCHEMA_VERSION` и
заведён). Поэтому здесь используется ТОЛЬКО чистый движок
(`bot.services.teto_slot_engine`, без БД/сети/денег): сиды сканируются
настоящим `random.Random(seed)`, найденный спин прогоняется с трейсом, и в
файл уходит ПОЛНЫЙ симулированный ответ роута — та же форма, что собирает
`api/routes/games.py::post_teto_slots`:

    {"game", "bet", "payout", "outcome", "user_balance_after", "bank_capped",
     "animation"}

со ставкой BET=500 (bet_per_line = 500 // TOTAL_LINES = 10) и условным
балансом "до спина" BASE_BALANCE (деньги в фикстурах — арифметика роута:
`user_balance_after = BASE_BALANCE - bet + payout`, `bank_capped = payout <
outcome.total_payout`, `animation.payout_paid == payout`).

Шесть сценариев (tests/fixtures/teto_animation/*.json, сиды — в README.md
рядом с ними):
  lose.json           — нулевая выплата, минимум op'ов (fill/evaluate/
                        drill_hunt(fired=false)/round_end).
  simple_win.json     — выигрыш базового раунда, 1-3 тумбл-шага, без фриспинов
                        и без срабатывания Дрель-Ханта.
  drill_hunt_win.json — базовый раунд, где Дрель-Хант СРАБОТАЛ и его волна
                        выиграла (`drill_hunt.fired=true`, пара evaluate+tumble
                        с source="drill_hunt_wave").
  freespins.json      — >=3 фриспин-раундов и хотя бы один пересечённый порог
                        лестницы (`round_end.multiplier_applied > 1` — Вариант
                        Y: множитель виден в раунде, СЛЕДУЮЩЕМ за пересечением).
  bank_capped.json    — тот же спин, что freespins.json, но выплата урезана
                        "банком" (D-06): `paid = total_payout // 2`, конверт
                        собран честно через `serialize_animation(trace,
                        paid_total=paid)` — НЕ правкой готового JSON; проверяет
                        контракт счётчика min(prefix_sum, payout_paid).
  replay.json         — тот же спин, что simple_win.json, но `animation: null`
                        (идемпотентный replay: `compute()` не вызывался,
                        финальная доска рисуется из `outcome`).

Перед записью каждая фикстура проверяется assert'ами: round-trip
json.dumps/loads без потерь, все op'ы — из словаря `TRACE_OPS` (5 имён), доска
каждого op'а — валидная партиция 36 клеток 6x6, трейс не усечён, и денежный
инвариант: сумма `round_end.final_round_payout` == `payout_engine_total` ==
`outcome.total_payout` (а `payout_paid` == top-level `payout`).

Перегенерация (из корня репозитория; сканирование сидов детерминировано,
поэтому повторный запуск даёт побайтово те же файлы):
    python3 scripts/gen_teto_fixtures.py
Скан freespins-сценария может занять минуты (фриспин-триггер ~1/80, пересечение
порога — реже; движок ~1.6k спинов/с) — прогресс печатается по ходу.
"""

from __future__ import annotations

import json
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.services import teto_slot_engine as eng  # noqa: E402

BET = 500
BET_PER_LINE = BET // eng.TOTAL_LINES  # 500 // 50 == 10
assert BET % eng.TOTAL_LINES == 0 and BET_PER_LINE == 10

# Условный баланс игрока ДО спина — только чтобы `user_balance_after` в
# фикстурах был правдоподобным и внутренне согласованным с `payout`.
BASE_BALANCE = 10_000

MAX_SEEDS = 500_000
PROGRESS_EVERY = 20_000

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "teto_animation"


# --- Предикаты сценариев (по "сырому" результату play_one_spin, без трейса) --


def _is_lose(res: dict) -> bool:
    return (
        res["total_payout"] == 0
        and res["freespins_played"] == 0
        and not res["base_round"]["drill_hunt_fired"]
    )


def _is_simple_win(res: dict) -> bool:
    br = res["base_round"]
    return (
        res["total_payout"] > 0
        and res["freespins_awarded"] == 0
        and not br["drill_hunt_fired"]
        and 1 <= br["tumble_steps_used"] <= 3
    )


def _is_drill_hunt_win(res: dict) -> bool:
    br = res["base_round"]
    return (
        res["freespins_awarded"] == 0
        and br["drill_hunt_fired"]
        and br["drill_hunt_wave_fired"]
    )


def _is_freespins_with_multiplier(res: dict) -> bool:
    if res["freespins_played"] < 3 or res["total_payout"] < 2:
        return False
    # `multiplier_applied` раунда i+1 == `ladder_multiplier_after` раунда i
    # (Вариант Y), поэтому порог "виден" в round_end только если ПОСЛЕ
    # пересёкшего раунда сыгран ещё хотя бы один.
    recs = res["fs_round_records"]
    return any(rec["ladder_multiplier_after"] > 1 for rec in recs[:-1])


SCENARIO_PREDICATES = {
    "lose": _is_lose,
    "simple_win": _is_simple_win,
    "drill_hunt_win": _is_drill_hunt_win,
    "freespins": _is_freespins_with_multiplier,
}


def scan_seeds() -> dict[str, int]:
    """Один детерминированный проход по сидам 0..MAX_SEEDS: для каждого
    сценария запоминается ПЕРВЫЙ подошедший сид (перезапуск скрипта даёт те же
    сиды => те же файлы). Скан идёт БЕЗ трейса (RNG потребляется бит-в-бит так
    же, см. докстринг `play_one_spin`) — трейс снимается вторым прогоном того
    же сида уже только для победителей."""
    found: dict[str, int] = {}
    t0 = time.monotonic()
    for seed in range(MAX_SEEDS):
        if len(found) == len(SCENARIO_PREDICATES):
            break
        res = eng.play_one_spin(random.Random(seed), BET_PER_LINE)
        for name, pred in SCENARIO_PREDICATES.items():
            if name not in found and pred(res):
                found[name] = seed
                print(f"[scan] {name}: seed={seed}")
        if seed and seed % PROGRESS_EVERY == 0:
            rate = seed / (time.monotonic() - t0)
            pending = sorted(set(SCENARIO_PREDICATES) - set(found))
            print(f"[scan] seed={seed} ({rate:.0f} спинов/с), ещё ищем: {pending}")
    missing = sorted(set(SCENARIO_PREDICATES) - set(found))
    if missing:
        raise SystemExit(f"не найдены сценарии {missing} за {MAX_SEEDS} сидов")
    return found


# --- Сборка ответа роута (симуляция post_teto_slots поверх чистого движка) ---


def run_with_trace(seed: int) -> tuple[dict, list]:
    trace: list = []
    res = eng.play_one_spin(random.Random(seed), BET_PER_LINE, trace=trace)
    return res, trace


def build_response(res: dict, trace: list, *, paid: int | None = None,
                   with_animation: bool = True) -> dict:
    """Полный симулированный ответ роута. `paid=None` — банк не капнул
    (`payout == outcome.total_payout`); иначе `paid` — фактическая выплата
    после капа (D-06). `with_animation=False` — контракт replay
    (`animation: null`, форма ответа та же)."""
    outcome = eng.serialize_spin_result(res)
    payout = outcome["total_payout"] if paid is None else paid
    return {
        "game": "teto_slots",
        "bet": BET,
        "payout": payout,
        "outcome": outcome,
        "user_balance_after": BASE_BALANCE - BET + payout,
        # то же выражение, что в роуте: payout < outcome.total_payout
        "bank_capped": payout < outcome["total_payout"],
        "animation": eng.serialize_animation(trace, paid_total=payout) if with_animation else None,
    }


# --- Валидация перед записью -------------------------------------------------


def _assert_valid_board(blocks: list[dict]) -> None:
    cells: set[tuple[int, int]] = set()
    for b in blocks:
        for key in ("block_id", "symbol_id", "row", "col", "height", "width"):
            assert key in b, f"в блоке нет ключа {key}: {b}"
        for r in range(b["row"], b["row"] + b["height"]):
            for c in range(b["col"], b["col"] + b["width"]):
                assert 0 <= r < 6 and 0 <= c < 6, f"клетка вне 6x6: {b}"
                assert (r, c) not in cells, f"перекрытие блоков в ({r},{c})"
                cells.add((r, c))
    assert len(cells) == 36, f"партиция покрывает {len(cells)} клеток из 36"


def validate_fixture(name: str, fixture: dict) -> None:
    # Форма верхнего уровня — ровно контракт роута.
    assert list(fixture) == [
        "game", "bet", "payout", "outcome", "user_balance_after", "bank_capped", "animation",
    ], f"{name}: неожиданный набор ключей {list(fixture)}"
    assert fixture["game"] == "teto_slots" and fixture["bet"] == BET
    assert fixture["user_balance_after"] == BASE_BALANCE - BET + fixture["payout"]

    # Round-trip: после dumps/loads не должно измениться НИЧЕГО (заодно ловит
    # незаметные не-JSON типы вроде tuple/set, которые dumps молча переделал бы).
    assert json.loads(json.dumps(fixture)) == fixture, f"{name}: json round-trip неточен"

    anim = fixture["animation"]
    outcome = fixture["outcome"]
    if anim is None:
        return

    assert anim["version"] == eng.TRACE_SCHEMA_VERSION
    assert anim["payout_paid"] == fixture["payout"]
    assert anim["payout_engine_total"] == outcome["total_payout"]
    assert anim["bank_capped"] == fixture["bank_capped"]
    # Реальные спины в усечение не упираются (см. TRACE_MAX_OPS/TRACE_MAX_ROUNDS)
    # — фикстура обязана быть полной, иначе денежный инвариант ниже не имеет смысла.
    assert anim["truncated"] is False and anim["truncated_reason"] is None
    assert anim["ops_recorded"] == anim["ops_total"] == len(anim["ops"])
    assert anim["rounds_recorded"] == anim["rounds_total"]

    rounds_sum = 0
    for i, op in enumerate(anim["ops"]):
        assert op["op"] in eng.TRACE_OPS, f"{name}: неизвестный op {op['op']!r}"
        assert op["seq"] == i
        assert op["phase"] in ("base", "freespin")
        assert isinstance(op["round"], int) and op["round"] >= 0
        _assert_valid_board(op["blocks"])
        if op["op"] == "round_end":
            rounds_sum += op["final_round_payout"]
    # Денежный инвариант: сумма финальных выплат раундов == движковый итог ==
    # outcome.total_payout (кап банком живёт ТОЛЬКО в payout/payout_paid).
    assert rounds_sum == anim["payout_engine_total"] == outcome["total_payout"], (
        f"{name}: {rounds_sum} != {anim['payout_engine_total']} != {outcome['total_payout']}"
    )


def _write(path: Path, fixture: dict) -> int:
    text = json.dumps(fixture, ensure_ascii=False, indent=2) + "\n"
    path.write_text(text, encoding="utf-8")
    return len(text.encode("utf-8"))


def main() -> None:
    seeds = scan_seeds()
    FIXTURES_DIR.mkdir(parents=True, exist_ok=True)

    fixtures: dict[str, dict] = {}
    notes: dict[str, str] = {}

    # 1-4: честные некапнутые ответы по найденным сидам.
    for name in ("lose", "simple_win", "drill_hunt_win", "freespins"):
        res, trace = run_with_trace(seeds[name])
        fixtures[name] = build_response(res, trace)

    # 5: bank_capped — тот же спин, что freespins (несколько раундов => кап
    # виден СЕРЕДИНОЙ тиков счётчика, а не только финалом), выплата урезана до
    # половины честного итога; конверт собирается заново с капнутым paid_total.
    res, trace = run_with_trace(seeds["freespins"])
    total = res["total_payout"]
    paid = total // 2
    assert 0 < paid < total, f"нечем капать: total_payout={total}"
    fixtures["bank_capped"] = build_response(res, trace, paid=paid)
    seeds["bank_capped"] = seeds["freespins"]
    notes["bank_capped"] = f"paid={paid} из total={total}"

    # 6: replay — тот же спин, что simple_win, но animation: null (сохранённый
    # исход, compute() не вызывался).
    res, _trace = run_with_trace(seeds["simple_win"])
    fixtures["replay"] = build_response(res, _trace, with_animation=False)
    seeds["replay"] = seeds["simple_win"]

    # Сценарные проверки сверх общих (что каждый файл показывает то, что обещает).
    a = fixtures["lose"]["animation"]
    assert fixtures["lose"]["payout"] == 0 and a["ops_total"] <= 6
    a = fixtures["simple_win"]["animation"]
    assert fixtures["simple_win"]["payout"] > 0
    assert not any(op["op"] == "drill_hunt" and op["fired"] for op in a["ops"])
    assert not any(op["phase"] == "freespin" for op in a["ops"])
    assert 1 <= sum(1 for op in a["ops"] if op["op"] == "tumble") <= 3
    a = fixtures["drill_hunt_win"]["animation"]
    assert any(op["op"] == "drill_hunt" and op["fired"] and op["wave_fired"] for op in a["ops"])
    assert any(op["op"] == "evaluate" and op["source"] == "drill_hunt_wave" for op in a["ops"])
    assert any(op["op"] == "tumble" and op["source"] == "drill_hunt_wave" for op in a["ops"])
    a = fixtures["freespins"]["animation"]
    assert fixtures["freespins"]["outcome"]["freespins_played"] >= 3
    assert any(op["op"] == "round_end" and op["multiplier_applied"] > 1 for op in a["ops"])
    assert fixtures["bank_capped"]["bank_capped"] is True
    assert fixtures["bank_capped"]["animation"]["bank_capped"] is True
    assert fixtures["replay"]["animation"] is None

    sizes: dict[str, int] = {}
    for name, fixture in fixtures.items():
        validate_fixture(name, fixture)
        sizes[name] = _write(FIXTURES_DIR / f"{name}.json", fixture)

    fs_anim = fixtures["freespins"]["animation"]
    fs_outcome = fixtures["freespins"]["outcome"]
    max_mult = max(
        op["multiplier_applied"] for op in fs_anim["ops"] if op["op"] == "round_end"
    )
    notes["freespins"] = (
        f"{fs_anim['rounds_total']} раундов / {fs_anim['ops_total']} ops, "
        f"множитель до x{max_mult} (лестница до x{fs_outcome['ladder_final_state']['multiplier']})"
    )

    readme_lines = [
        "# Фикстуры анимации Тето-слота (`POST /api/v1/games/teto_slots`)",
        "",
        "НАСТОЯЩИЕ ответы чистого движка (`bot/services/teto_slot_engine.py`), не",
        "рукописный JSON: каждый файл — полный симулированный ответ роута",
        "`{game, bet, payout, outcome, user_balance_after, bank_capped, animation}`",
        f"со ставкой {BET} (bet_per_line={BET_PER_LINE}) и балансом до спина {BASE_BALANCE}.",
        "Для dev-моков экрана слота и Playwright/визуального QA.",
        "",
        "Перегенерация (детерминирована, даёт те же файлы):",
        "",
        "    python3 scripts/gen_teto_fixtures.py",
        "",
        "| Файл | Сид | Что показывает |",
        "|---|---|---|",
    ]
    descriptions = {
        "lose": "проигрыш: payout=0, минимум op'ов",
        "simple_win": "выигрыш базового раунда, 1-3 тумбл-шага, без фриспинов/Дрель-Ханта",
        "drill_hunt_win": "Дрель-Хант сработал, волна выиграла (source=\"drill_hunt_wave\")",
        "freespins": f"фриспины с порогом лестницы: {notes['freespins']}",
        "bank_capped": (
            f"тот же спин, что freespins, но банк урезал выплату ({notes['bank_capped']}); "
            "проверка счётчика min(prefix_sum, payout_paid)"
        ),
        "replay": "тот же спин, что simple_win, но `animation: null` (идемпотентный replay)",
    }
    for name in ("lose", "simple_win", "drill_hunt_win", "freespins", "bank_capped", "replay"):
        readme_lines.append(f"| `{name}.json` | {seeds[name]} | {descriptions[name]} |")
    readme_lines += [
        "",
        "`bank_capped.json` — единственный файл, где `payout` <",
        "`outcome.total_payout` (симуляция капа банком D-06); собран честно через",
        "`serialize_animation(trace, paid_total=paid)`, JSON после сериализации не",
        "правился.",
        "",
    ]
    (FIXTURES_DIR / "README.md").write_text("\n".join(readme_lines), encoding="utf-8")

    print("\n--- итог ---")
    for name in ("lose", "simple_win", "drill_hunt_win", "freespins", "bank_capped", "replay"):
        extra = f" ({notes[name]})" if name in notes else ""
        print(f"{name}.json: seed={seeds[name]}, {sizes[name]} байт{extra}")


if __name__ == "__main__":
    main()
