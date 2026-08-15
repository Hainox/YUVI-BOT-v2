# Фикстуры анимации Тето-слота (`POST /api/v1/games/teto_slots`)

НАСТОЯЩИЕ ответы чистого движка (`bot/services/teto_slot_engine.py`), не
рукописный JSON: каждый файл — полный симулированный ответ роута
`{game, bet, payout, outcome, user_balance_after, bank_capped, animation}`
со ставкой 500 (bet_per_line=10) и балансом до спина 10000.
Для dev-моков экрана слота и Playwright/визуального QA.

Перегенерация (детерминирована, даёт те же файлы):

    python3 scripts/gen_teto_fixtures.py

| Файл | Сид | Что показывает |
|---|---|---|
| `lose.json` | 0 | проигрыш: payout=0, минимум op'ов |
| `simple_win.json` | 4 | выигрыш базового раунда, 1-3 тумбл-шага, без фриспинов/Дрель-Ханта |
| `drill_hunt_win.json` | 6 | Дрель-Хант сработал, волна выиграла (source="drill_hunt_wave") |
| `freespins.json` | 4859 | фриспины с порогом лестницы: 14 раундов / 102 ops, множитель до x2 (лестница до x2) |
| `bank_capped.json` | 4859 | тот же спин, что freespins, но банк урезал выплату (paid=9365 из total=18730); проверка счётчика min(prefix_sum, payout_paid) |
| `replay.json` | 4 | тот же спин, что simple_win, но `animation: null` (идемпотентный replay) |

`bank_capped.json` — единственный файл, где `payout` <
`outcome.total_payout` (симуляция капа банком D-06); собран честно через
`serialize_animation(trace, paid_total=paid)`, JSON после сериализации не
правился.
