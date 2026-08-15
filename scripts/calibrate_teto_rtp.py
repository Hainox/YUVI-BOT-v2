"""Monte-Carlo калибровка RTP слота "Тето Брейнрот: Дрель-Хант".

Зачем этот скрипт существует: прецедент D-06 (`bot/data/slot_data.py`) —
непроверенные черновые цифры паутейбла один раз уже дали сломанный RTP ~38-40%
на Azumanga, и roadmap.md прямо требует Monte-Carlo прогон паутейбла/весов до
продакшена для КАЖДОГО нового слота. Здесь тот же принцип, что и
`tests/test_slot_engine.py::test_sampled_rtp_in_band`, но вынесенный в
отдельный инструмент: тест держит только грубую полосу допуска (быстрый,
на каждом CI-прогоне), а этот скрипт — рабочая лошадка калибровки с полной
статистикой (hit rate, частота фриспинов, вклад базы/бонуса, хвосты выплат,
срабатывания хардкапов), которую гоняют руками при каждой правке
`TETO_PAYTABLE`/`WEIGHTS`/`SHAPE_POOL`/`TETO_PAYLINES`.

Чистый движок, без БД/сети — только `bot.services.teto_slot_engine`.

Запуск (из корня репозитория):
    python3 scripts/calibrate_teto_rtp.py --spins 100000 --bet-per-line 10 --seed 1
"""

from __future__ import annotations

import argparse
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.services import teto_slot_engine as eng  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spins", type=int, default=50_000)
    parser.add_argument("--bet-per-line", type=int, default=10)
    parser.add_argument("--seed", type=int, default=1)
    args = parser.parse_args()

    bet_per_line = args.bet_per_line
    bet_total = bet_per_line * eng.TOTAL_LINES

    total_bet = 0
    total_won = 0
    base_won = 0
    fs_won = 0
    hits = 0
    fs_triggers = 0
    fs_rounds_played = 0
    tumble_cap_hits = 0
    fs_cap_hits = 0
    max_win_x = 0.0
    payouts: list[int] = []

    t0 = time.monotonic()
    for i in range(args.spins):
        rng = random.Random(args.seed * 1_000_003 + i)
        result = eng.play_one_spin(rng, bet_per_line)

        total_bet += bet_total
        won = result["total_payout"]
        total_won += won
        payouts.append(won)
        if won > 0:
            hits += 1
        base_won += result["base_round"]["raw_round_payout"]
        fs_won += won - result["base_round"]["raw_round_payout"]
        if result["freespins_awarded"] > 0:
            fs_triggers += 1
        fs_rounds_played += result["freespins_played"]
        if result["base_round"]["tumble_hard_cap_hit"]:
            tumble_cap_hits += 1
        if result["freespins_hard_cap_hit"]:
            fs_cap_hits += 1
        max_win_x = max(max_win_x, won / bet_total)

        if (i + 1) % 10_000 == 0:
            elapsed = time.monotonic() - t0
            print(
                f"  {i + 1}/{args.spins}  RTP пока: {total_won / total_bet:.4f}  "
                f"({elapsed:.0f}с, {(i + 1) / elapsed:.0f} спин/с)",
                flush=True,
            )

    n = args.spins
    payouts.sort()

    def pct(p: float) -> int:
        return payouts[min(n - 1, int(n * p))]

    rtp = total_won / total_bet
    print()
    print(f"спинов: {n}, ставка: {bet_total} ({bet_per_line}/линию x {eng.TOTAL_LINES} линий)")
    print(f"RTP:                {rtp:.4f}")
    print(f"  вклад базы:       {base_won / total_bet:.4f}")
    print(f"  вклад фриспинов:  {fs_won / total_bet:.4f}")
    print(f"hit rate (won>0):   {hits / n:.4f}")
    print(f"частота фриспинов:  {fs_triggers / n:.5f} (1 к {n / max(1, fs_triggers):.0f})")
    print(f"фриспин-раундов/спин: {fs_rounds_played / n:.4f}")
    print(f"tumble-кап (база):  {tumble_cap_hits} ({tumble_cap_hits / n:.5f})")
    print(f"фриспин-кап:        {fs_cap_hits}")
    print(f"выплата p50/p90/p99/p999: {pct(0.50)} / {pct(0.90)} / {pct(0.99)} / {pct(0.999)}")
    print(f"макс выигрыш:       {payouts[-1]} ({max_win_x:.1f}x ставки)")


if __name__ == "__main__":
    main()
