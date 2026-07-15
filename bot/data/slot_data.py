"""Данные слота "Azumanga" (04.1-02) — server-authoritative порт
`webapp/slot-data.jsx` (клиентский прототип с Mini App, вычислявший исход
через `Math.random()` — недопустимо для реальных ювиков, T-04.1-05).

Источник истины по числам — `.planning/phases/04-mini-app-games-mini-app/04-CONTEXT.md`:
- D-05: 8 символов + веса (`WEIGHTS`) — переносятся из черновика БЕЗ изменений.
- D-06: паутейбл (`PAYTABLE`) — ПЕРЕСЧИТАН Monte-Carlo симуляцией (множитель
  ~x2.4 от исходных цифр черновика) до RTP ~92.78%. Исходные числа черновика
  (`webapp/slot-data.jsx::SYMBOLS[...].pay`, например muscle 50/200/1000)
  давали сломанный RTP ~38.5-39.9% и НЕ используются здесь — не путать при
  правках.

Сетка/линии/скаттер-фриспины/wild-замещение — без изменений от D-05.
"""

from __future__ import annotations

# --- Символы (D-05: роль/вес/визуал переносятся из webapp/slot-data.jsx) -----

SYMBOLS: dict[str, dict] = {
    "muscle": {"role": "wild", "weight": 2, "tint": "#ffd84a", "name": "КАЧОК-ОСАКА"},
    "keffiyeh": {"role": "scatter", "weight": 2, "tint": "#ff5b8d", "name": "ШЕЙХ-АКА"},
    "gasp": {"role": "high", "weight": 4, "tint": "#7be6ff", "name": "НИХУЯ"},
    "lightning-eyes": {"role": "high", "weight": 5, "tint": "#c4a8ff", "name": "Osaka KYS"},
    "dog": {"role": "mid", "weight": 7, "tint": "#ffb1c8", "name": "Bruh…."},
    "osaka-stand": {"role": "low", "weight": 10, "tint": "#ffe27a", "name": "Гроши заработал"},
    "bath-chibi": {"role": "low", "weight": 9, "tint": "#b8e7ff", "name": "Да-да, выиграл хуйню"},
    "sakaki": {"role": "low", "weight": 8, "tint": "#d6c4a3", "name": "WTF OSAKA NIG…."},
}

WEIGHTS: dict[str, int] = {symbol_id: data["weight"] for symbol_id, data in SYMBOLS.items()}

# --- Паутейбл (D-06 REBALANCED — RTP ~92.78%, НЕ исходные цифры черновика) --

PAYTABLE: dict[str, dict[int, int]] = {
    "muscle": {3: 120, 4: 480, 5: 2400},
    "keffiyeh": {3: 0, 4: 0, 5: 0},  # платит только фриспинами, см. FREESPIN_TABLE
    "gasp": {3: 24, 4: 65, 5: 173},
    "lightning-eyes": {3: 22, 4: 48, 5: 120},
    "dog": {3: 12, 4: 26, 5: 58},
    "osaka-stand": {3: 10, 4: 24, 5: 48},
    "bath-chibi": {3: 7, 4: 19, 5: 36},
    "sakaki": {3: 7, 4: 14, 5: 29},
}

# --- Роли wild/scatter --------------------------------------------------------

WILD_ID = "muscle"
SCATTER_ID = "keffiyeh"

# --- Фриспины по количеству scatter (D-05, без изменений) --------------------

FREESPIN_TABLE: dict[int, int] = {3: 4, 4: 6, 5: 7}

# --- 10 стандартных paylines на сетке 3x5 (webapp/slot-data.jsx::PAYLINES) --
# Каждая линия — индекс строки (0..2) на каждый из 5 столбцов.

PAYLINES: list[list[int]] = [
    [1, 1, 1, 1, 1],  # 1 · middle
    [0, 0, 0, 0, 0],  # 2 · top
    [2, 2, 2, 2, 2],  # 3 · bottom
    [0, 1, 2, 1, 0],  # 4 · V
    [2, 1, 0, 1, 2],  # 5 · Λ
    [0, 0, 1, 2, 2],  # 6 · descending
    [2, 2, 1, 0, 0],  # 7 · ascending
    [1, 0, 0, 0, 1],  # 8 · top U
    [1, 2, 2, 2, 1],  # 9 · bottom U
    [0, 1, 0, 1, 0],  # 10 · zigzag
]
