"""Бенчмарк `/q` (rewrite + answer) для сравнения gpt-5.6-luna и xAI Grok.

Что измеряет:
- rewrite (Шаг 1 /q): ровно три разговорных переформулировки вопроса;
- answer (Шаг 6 /q): финальный ответ по фрагментам истории чата.

Метрики на каждый вызов:
- latency (сек), prompt/completion токены (из usage), стоимость (USD) по
  официальным ценам;
- эвристики качества (детерминированные, формат/факты-якоря);
- LLM-as-judge (опционально, для answer): семантическая оценка по рубрике
  relevance / grounding / style / usefulness, модель-судья независима от
  сравниваемых (по умолчанию minimax-m2.5).

Провайдеры конфигурируются через окружение:
- OPENAI_API_KEY + OPENAI_BASE_URL — OpenCode Zen (gpt-5.6-luna, grok-4.6);
- XAI_API_KEY (опционально) — прямой xAI https://api.x.ai/v1 (grok-4.6).

Секреты читаются из .env и НИКОГДА не печатаются. Результат — Markdown-таблица
качества/стоимости в stdout и JSON в scripts/q_bench_results.json.

Запуск:
    python scripts/q_bench.py
    python scripts/q_bench.py --runs 2 --models gpt-5.6-luna,grok-4.6
    python scripts/q_bench.py --no-judge
    python scripts/q_bench.py --judge-model glm-5
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
from pathlib import Path

import httpx
from dotenv import load_dotenv

# Корень репозитория, чтобы dotenv и `import bot.*` работали независимо от cwd.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")
sys.path.insert(0, str(PROJECT_ROOT))

from bot.services.q_service import Q_ANSWER_SYSTEM_PROMPT  # noqa: E402
from bot.services.q_service import Q_REWRITE_SYSTEM_PROMPT  # noqa: E402

RESULTS_PATH = Path(__file__).resolve().parent / "q_bench_results.json"

# Цены за 1M токенов (input, output), USD. Источники:
# - gpt-5.6-luna: https://opencode.ai/docs/zen/ (≤272K токенов)
# - grok-4.6:    https://docs.x.ai/developers/pricing (≤200K токенов)
# - minimax-m2.5 (судья): https://opencode.ai/docs/zen/
PRICING: dict[str, tuple[float, float]] = {
    "gpt-5.6-luna": (0.20, 1.20),
    "grok-4.6": (2.00, 6.00),
    "grok-4.5": (2.00, 6.00),
    "minimax-m2.5": (0.30, 1.20),
    "glm-5": (1.00, 3.20),
}

JUDGE_CRITERIA = ("relevance", "grounding", "style", "usefulness")

JUDGE_SYSTEM_PROMPT = (
    "Ты — строгий судья качества ответа ассистента в русскоязычном групповом чате. "
    "Тебе даны: ВОПРОС пользователя, ФРАГМЕНТЫ истории чата (единственный источник "
    "фактов) и ОТВЕТ модели. Оцени ответ целыми числами от 1 (плохо) до 5 (отлично) "
    "по критериям:\n"
    "- relevance: отвечает по существу, не уходит в сторону;\n"
    "- grounding: опирается на факты фрагментов, не выдумывает и не приписывает лишнего;\n"
    "- style: стиль чата — без эмодзи, без вступлений «конечно»/«отличный вопрос», "
    "без цензурных эвфемизмов;\n"
    "- usefulness: ответ конкретен и полезен участнику.\n"
    'Верни ТОЛЬКО JSON без пояснений: {"relevance": N, "grounding": N, "style": N, "usefulness": N}'
)


# --- Репрезентативные входы /q (не зависят от БД) ---------------------------


def _rewrite_cases() -> list[str]:
    return [
        "что советовал по видеокарте",
        "когда договаривались про стрим в дискорде",
        "кто обещал вернуть ювики до завтра",
        "какой слот вчера хвалили",
    ]


def _answer_cases() -> list[tuple[str, str, list[str]]]:
    cases = [
        (
            "@Hoouka что советовал по видеокарте",
            (
                "· 2023-12-27 13:59 МСК @Hoouka: Комп зато доставить просто будет\n"
                "★[sim=0.98] 2023-12-27 14:01 МСК @Hoouka: А где видеокарта кста\n"
                "· 2023-12-27 14:01 МСК @Hoouka: Или ещё не приехала?\n"
                "· 2023-12-27 14:06 МСК @McFreezy: У брата в пк\n"
                "★[sim=0.95] 2023-12-27 14:07 МСК @Hoouka: Взял 4070 в итоге\n"
            ),
            ["4070"],
        ),
        (
            "когда собираемся в дискорде",
            (
                "· 2026-08-10 19:02 МСК @kto: завтра кто в диск?\n"
                "★[sim=0.96] 2026-08-10 19:03 МСК @McFreezy: в девять вечера\n"
                "· 2026-08-10 19:03 МСК @kto: по мск?\n"
                "· 2026-08-10 19:04 МСК @McFreezy: да, в девять по мск\n"
            ),
            ["девять", "мск"],
        ),
        (
            "кто должен ювики",
            (
                "★[sim=0.97] 2026-08-12 20:15 МСК @lurker: займи сотку до завтра\n"
                "· 2026-08-12 20:16 МСК @McFreezy: держи\n"
                "· 2026-08-12 20:17 МСК @lurker: завтра зарплата, отдам\n"
            ),
            ["lurker", "завтра"],
        ),
        (
            "что за новый слот",
            (
                "★[sim=0.94] 2026-08-14 18:40 МСК @gambler: слот тето вообще топ\n"
                "· 2026-08-14 18:41 МСК @gambler: крутил вчера, отдаёт норм\n"
                "★[sim=0.92] 2026-08-14 18:42 МСК @McFreezy: джекпот там жирный\n"
            ),
            ["тето"],
        ),
    ]
    return [(q, ctx, anchors) for q, ctx, anchors in cases]


# --- Вызов модели ------------------------------------------------------------


async def _call(
    client: httpx.AsyncClient,
    base_url: str,
    api_key: str,
    model: str,
    messages: list[dict],
    max_tokens: int,
    reasoning_effort: str | None = None,
) -> dict:
    t0 = time.monotonic()
    payload: dict = {
        "model": model,
        "messages": messages,
        "stream": False,
        "max_tokens": max_tokens,
        "temperature": 0,
    }
    # У Grok reasoning нельзя отключить (docs.x.ai); effort влияет на объём
    # скрытых reasoning-токенов, которые всё равно тарифицируются как output.
    if reasoning_effort and model.startswith("grok"):
        payload["reasoning_effort"] = reasoning_effort
    try:
        resp = await client.post(
            f"{base_url.rstrip('/')}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json=payload,
            timeout=70.0,
        )
    except Exception as exc:  # noqa: BLE001
        return {
            "status": "exception",
            "error": f"{type(exc).__name__}: {exc}"[:200],
            "latency": round(time.monotonic() - t0, 2),
        }
    latency = round(time.monotonic() - t0, 2)
    if resp.status_code != 200:
        return {"status": resp.status_code, "error": resp.text[:300], "latency": latency}
    data = resp.json()
    content = ""
    usage = {}
    try:
        content = data["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError):
        pass
    usage = data.get("usage") or {}
    return {
        "status": 200,
        "latency": latency,
        "content": content,
        "prompt_tokens": usage.get("prompt_tokens", 0),
        "completion_tokens": usage.get("completion_tokens", 0),
    }


# --- Эвристики качества ------------------------------------------------------


def _score_rewrite(content: str, question: str) -> tuple[int, int, str]:
    """(балл, максимум, пояснение): 3 чистых варианта без нумерации/кавычек."""
    lines = [ln for ln in (line.strip() for line in content.splitlines()) if ln]
    cleaned: list[str] = []
    for ln in lines:
        ln = ln.lstrip("-*•").strip()
        ln = ln.split(". ", 1)[-1].split(") ", 1)[-1]
        ln = ln.strip().strip('"“”«»')
        if ln and ln.casefold() != question.casefold():
            cleaned.append(ln)
    score = 0
    notes = []
    if len(cleaned) >= 3:
        score += 1
    else:
        notes.append(f"вариантов {len(cleaned)}/3")
    if len(cleaned) <= 3:
        score += 1
    else:
        notes.append(f"лишних строк {len(cleaned) - 3}")
    if cleaned and all("\n" not in c for c in cleaned):
        score += 1
    if not notes:
        notes.append("ок")
    return score, 3, "; ".join(notes)


def _score_answer(content: str, anchors: list[str]) -> tuple[int, int, str]:
    """(балл, максимум, пояснение): стиль /q + удержание фактов контекста."""
    score = 0
    notes = []
    low = content.casefold()
    if content.strip():
        score += 1
    else:
        notes.append("пусто")
    if "в чате этого нет" not in low and "в чате нет" not in low:
        score += 1
    else:
        notes.append("отписка про отсутствие")
    if not any(e in content for e in ("😀", "👍", "🔥", "✅")):
        score += 1
    else:
        notes.append("эмодзи")
    for opener in ("конечно", "отличный вопрос", "хороший вопрос"):
        if content.casefold().startswith(opener):
            notes.append(f"вступление «{opener}»")
            break
    else:
        score += 1
    if anchors:
        hits = sum(1 for a in anchors if a.casefold() in low)
        score += 1 if hits == len(anchors) else 0
        if hits != len(anchors):
            notes.append(f"факты {hits}/{len(anchors)}")
    if not notes:
        notes.append("ок")
    return score, 5 if anchors else 4, "; ".join(notes)


# --- LLM-as-judge ------------------------------------------------------------


def _parse_judge(content: str) -> dict[str, int] | None:
    """Парсит JSON-вердикт судьи; падает к regex, если модель обернула JSON."""
    data: dict | None = None
    try:
        parsed = json.loads(content)
        if isinstance(parsed, dict):
            data = parsed
    except json.JSONDecodeError:
        data = None

    out: dict[str, int] = {}
    for key in JUDGE_CRITERIA:
        value: int | float | None = None
        if data and isinstance(data.get(key), (int, float)):
            value = data[key]
        if value is None:
            match = re.search(rf'"{key}"\s*:\s*(\d+)', content)
            if match:
                value = int(match.group(1))
        if value is not None:
            out[key] = max(1, min(5, int(value)))
    return out or None


async def _judge_answer(
    client: httpx.AsyncClient,
    cfg: dict,
    judge_model: str,
    question: str,
    context: str,
    answer: str,
) -> dict:
    """Семантическая оценка ответа независимой моделью-судьёй."""
    user_prompt = (
        f"ВОПРОС:\n{question}\n\nФРАГМЕНТЫ:\n{context}\n\nОТВЕТ МОДЕЛИ:\n{answer}"
    )
    # 1200, а не 300: у reasoning-моделей (minimax/glm) скрытый reasoning
    # съедает бюджет до первого символа content — при 300 судья возвращает пусто.
    res = await _call(
        client,
        cfg["base_url"],
        cfg["api_key"],
        judge_model,
        [
            {"role": "system", "content": JUDGE_SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        1200,
    )
    if res["status"] != 200:
        return {**res, "scores": None, "composite": None, "cost_usd": 0.0}
    scores = _parse_judge(res.get("content", ""))
    composite = round(sum(scores.values()) / len(scores), 2) if scores else None
    price_in, price_out = PRICING.get(judge_model, (0.0, 0.0))
    cost = (
        res.get("prompt_tokens", 0) / 1e6 * price_in
        + res.get("completion_tokens", 0) / 1e6 * price_out
    )
    return {
        **res,
        "scores": scores,
        "composite": composite,
        "cost_usd": round(cost, 4),
    }


# --- Прогон ------------------------------------------------------------------


async def _run_case(
    client: httpx.AsyncClient,
    cfg: dict,
    model: str,
    kind: str,
    messages: list[dict],
    max_tokens: int,
    scorer,
) -> dict:
    res = await _call(
        client,
        cfg["base_url"],
        cfg["api_key"],
        model,
        messages,
        max_tokens,
        reasoning_effort=cfg.get("reasoning_effort"),
    )
    row = {**res, "kind": kind}
    if res["status"] == 200:
        row["score"], row["max_score"], row["notes"] = scorer(res["content"])
    return row


async def _run_model(
    model: str,
    cfg: dict,
    runs: int,
    judge_model: str | None,
) -> dict[str, list[dict]]:
    async with httpx.AsyncClient() as client:
        out: dict[str, list[dict]] = {"rewrite": [], "answer": []}
        for _ in range(runs):
            for q in _rewrite_cases():
                out["rewrite"].append(
                    await _run_case(
                        client,
                        cfg,
                        model,
                        "rewrite",
                        [
                            {"role": "system", "content": Q_REWRITE_SYSTEM_PROMPT},
                            {"role": "user", "content": q},
                        ],
                        300,
                        lambda c: _score_rewrite(c, q),
                    )
                )
            for q, ctx, anchors in _answer_cases():
                row = await _run_case(
                    client,
                    cfg,
                    model,
                    "answer",
                    [
                        {"role": "system", "content": Q_ANSWER_SYSTEM_PROMPT},
                        {
                            "role": "user",
                            "content": (
                                f"ВОПРОС ПОЛЬЗОВАТЕЛЯ: {q}\n"
                                f"НАЙДЕННЫЕ СООБЩЕНИЯ (★ релевантных: 2, всего: 5):\n{ctx}"
                            ),
                        },
                    ],
                    1500,
                    lambda c: _score_answer(c, anchors),
                )
                if judge_model and row["status"] == 200 and row.get("content"):
                    row["judge"] = await _judge_answer(
                        client, cfg, judge_model, q, ctx, row["content"]
                    )
                out["answer"].append(row)
        return out


def _aggregate(rows: list[dict], model: str) -> dict:
    ok = [r for r in rows if r["status"] == 200]
    n = len(rows)
    scored = [r for r in ok if "score" in r]
    pts = sum(r["score"] for r in scored)
    max_pts = sum(r["max_score"] for r in scored)
    pt_in = sum(r.get("prompt_tokens", 0) for r in ok)
    pt_out = sum(r.get("completion_tokens", 0) for r in ok)
    price_in, price_out = PRICING.get(model, (0.0, 0.0))
    cost = pt_in / 1e6 * price_in + pt_out / 1e6 * price_out

    judges = [r["judge"] for r in ok if r.get("judge") and r["judge"].get("composite") is not None]
    judge_composite = (
        round(sum(j["composite"] for j in judges) / len(judges), 2) if judges else None
    )
    judge_criteria: dict[str, float] = {}
    for name in JUDGE_CRITERIA:
        vals = [j["scores"][name] for j in judges if j.get("scores") and name in j["scores"]]
        if vals:
            judge_criteria[name] = round(sum(vals) / len(vals), 2)
    judge_cost = round(sum(j.get("cost_usd", 0.0) for j in judges), 4)

    return {
        "success": len(ok),
        "total": n,
        "quality_pct": round(pts / max_pts * 100, 1) if max_pts else 0.0,
        "avg_latency": round(sum(r["latency"] for r in ok) / len(ok), 2) if ok else 0.0,
        "prompt_tokens": pt_in,
        "completion_tokens": pt_out,
        "cost_usd": round(cost, 4),
        "judge_composite": judge_composite,
        "judge_criteria": judge_criteria,
        "judge_cost_usd": judge_cost,
        "judge_calls": len(judges),
    }


def _judge_cell(a: dict) -> str:
    if a.get("judge_composite") is None:
        return "—"
    criteria = a.get("judge_criteria") or {}
    letters = {"relevance": "r", "grounding": "g", "style": "s", "usefulness": "u"}
    parts = [f"{letters[k]}{v:.1f}" for k, v in criteria.items() if k in letters]
    return f"{a['judge_composite']} ({' '.join(parts)})"


def _render_table(agg: dict[str, dict[str, dict]]) -> str:
    lines = [
        "| Модель | Этап | Успех | Эвристики | Судья | Латентность | Токены (in/out) | Стоимость (USD) |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for model, kinds in agg.items():
        for kind, a in kinds.items():
            judge = _judge_cell(a) if kind == "answer" else "—"
            lines.append(
                f"| {model} | {kind} | {a['success']}/{a['total']} "
                f"| {a['quality_pct']}% | {judge} | {a['avg_latency']}s "
                f"| {a['prompt_tokens']}/{a['completion_tokens']} "
                f"| ${a['cost_usd']:.4f} |"
            )
    return "\n".join(lines)


async def main(
    models: list[str],
    runs: int,
    reasoning_effort: str | None,
    judge_model: str | None,
) -> None:
    zen_key = os.getenv("OPENAI_API_KEY", "")
    zen_url = os.getenv("OPENAI_BASE_URL", "https://opencode.ai/zen/v1").rstrip("/")
    xai_key = os.getenv("XAI_API_KEY", "")

    cfgs: dict[str, dict] = {}
    for model in models:
        if model.startswith("grok") and xai_key:
            cfgs[model] = {
                "base_url": "https://api.x.ai/v1",
                "api_key": xai_key,
                "label": "xAI",
                "reasoning_effort": reasoning_effort,
            }
        else:
            cfgs[model] = {
                "base_url": zen_url,
                "api_key": zen_key,
                "label": "OpenCode Zen",
                "reasoning_effort": reasoning_effort,
            }

    if not any(c["api_key"] for c in cfgs.values()):
        print(
            "Нет OPENAI_API_KEY/XAI_API_KEY в .env — поставьте ключ и повторите.\n"
            "grok-* без XAI_API_KEY поедет через OpenCode Zen (общий OPENAI_API_KEY)."
        )
        if not models:
            sys.exit(2)

    if judge_model and judge_model in models:
        print(
            f"Предупреждение: судья {judge_model} совпадает с кандидатом — "
            "оценка может быть смещена. Лучше указать третью модель (--judge-model)."
        )

    agg: dict[str, dict[str, dict]] = {}
    for model in models:
        cfg = cfgs[model]
        if not cfg["api_key"]:
            print(f"Пропущен {model}: нет ключа для {cfg['label']}")
            continue
        print(
            f"Бенчмарк {model} через {cfg['label']} ({cfg['base_url']})"
            + (f", судья {judge_model}" if judge_model else ", без судьи")
            + " ...",
            flush=True,
        )
        rows = await _run_model(model, cfg, runs, judge_model)
        agg[model] = {
            "rewrite": _aggregate(rows["rewrite"], model),
            "answer": _aggregate(rows["answer"], model),
        }

    print("\n" + _render_table(agg) + "\n")
    if judge_model:
        total_judge_cost = round(
            sum(k["answer"]["judge_cost_usd"] for k in agg.values()), 4
        )
        print(f"Судья: {judge_model} (независимая модель), суммарная стоимость — ${total_judge_cost}")
    Path(RESULTS_PATH).write_text(json.dumps(agg, ensure_ascii=False, indent=2))
    print(f"JSON: {RESULTS_PATH}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--models", default="gpt-5.6-luna,grok-4.6")
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument(
        "--reasoning-effort",
        choices=["low", "medium", "high"],
        default="low",
        help="reasoning_effort для grok-* (xAI не позволяет отключить reasoning)",
    )
    parser.add_argument(
        "--judge-model",
        default="minimax-m2.5",
        help="независимая модель-судья для оценки качества ответа",
    )
    parser.add_argument("--no-judge", action="store_true", help="отключить LLM-as-judge")
    args = parser.parse_args()
    models = [m.strip() for m in args.models.split(",") if m.strip()]
    judge_model = None if args.no_judge else args.judge_model
    asyncio.run(main(models, args.runs, args.reasoning_effort, judge_model))
