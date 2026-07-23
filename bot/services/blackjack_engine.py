"""Чистая (без DB/economy) карточная логика блэкджека (04.1-03) — колода,
подсчёт очков, доигровка дилера, определение исхода раздачи.

Никаких побочных эффектов и никакого собственного RNG здесь: `rng`
передаётся вызывающим (`casino_service._rng`, `secrets.SystemRandom()`) —
этот модуль сам не создаёт случайность, только оперирует уже переданным
генератором/колодой (T-04.1-08: колода server-authoritative, клиент
никогда не поставляет карты).

Правила (D-03 из `04-CONTEXT.md`):
- Натуральный блэкджек (2 карты, 21 очко) платит `BLACKJACK_NATURAL_MULT`
  (2.5x) — если у дилера тоже не натурал, иначе push.
- Обычный выигрыш — 2x, push — 1x (возврат ставки), проигрыш/перебор — 0x.
- Дилер добирает карты, пока сумма < 17; на "мягких" 17 (soft 17 — есть
  туз, считающийся как 11) дилер ОСТАНАВЛИВАЕТСЯ (S17, D-03) — не
  американский H17.

Ревизия 2026-07-23 (тема колоды «Мику × Тето», design project
29287ff0-7367-49ae-ba24-0bcb9553a6f9): токен карты — ранг + масть одним
символом-суффиксом ("A♠", "10♥"), масть раньше вообще не хранилась ("не
важна для очков"), но нужна фронтенду, чтобы решить, чей мем рисовать
(чёрные ♠♣ — Мику, красные ♥♦ — Тето, см. miniapp/src/lib/blackjackTheme.ts).
Вся arithmetic по-прежнему смотрит только на ранг через _rank_of() — масть
не участвует ни в подсчёте очков, ни в определении исхода, только в
рендере. Безопасно менять формат токена без миграции: на момент правки в
БД не было ни одной активной (status='active') раздачи блэкджека.
"""

from __future__ import annotations

RANKS = ["A", "2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K"]
SUITS = ["♠", "♣", "♥", "♦"]

_RANK_VALUES = {
    "A": 11,
    "2": 2,
    "3": 3,
    "4": 4,
    "5": 5,
    "6": 6,
    "7": 7,
    "8": 8,
    "9": 9,
    "10": 10,
    "J": 10,
    "Q": 10,
    "K": 10,
}


def _rank_of(card: str) -> str:
    """Масть — всегда последний символ токена (единственный не-ASCII
    символ), ранг — всё остальное. Работает и для "10♥" (2-значный ранг),
    и для "A♠" (1-значный)."""
    return card[:-1]


def new_shuffled_deck(rng) -> list[str]:
    """52-карточная колода (13 рангов x 4 настоящих масти, ранг+масть одним
    токеном), перетасованная через `rng` (server-authoritative
    `secrets.SystemRandom`, передан вызывающим — эта функция никогда не
    создаёт свой RNG)."""
    deck = [rank + suit for rank in RANKS for suit in SUITS]
    rng.shuffle(deck)
    return deck


def hand_value(cards: list[str]) -> tuple[int, bool]:
    """Лучшая сумма очков: тузы сначала считаются как 11, затем по одному
    понижаются до 1, чтобы избежать перебора. Возвращает (сумма, is_soft) —
    `is_soft` True, если хотя бы один туз в итоге всё ещё считается как 11."""
    total = sum(_RANK_VALUES[_rank_of(card)] for card in cards)
    aces = sum(1 for card in cards if _rank_of(card) == "A")
    while total > 21 and aces > 0:
        total -= 10
        aces -= 1
    is_soft = aces > 0
    return total, is_soft


def is_natural(cards: list[str]) -> bool:
    """True для двухкарточной руки на 21 очко (натуральный блэкджек)."""
    if len(cards) != 2:
        return False
    value, _ = hand_value(cards)
    return value == 21


def dealer_play(deck: list[str], dealer_cards: list[str]) -> tuple[list[str], list[str]]:
    """Дилер добирает карты, пока сумма < 17; стоит на любых 17, включая
    soft 17 (S17, D-03). Возвращает (обновлённая колода, финальные карты
    дилера) — не мутирует переданные списки."""
    deck = list(deck)
    dealer_cards = list(dealer_cards)
    while True:
        value, _ = hand_value(dealer_cards)
        if value >= 17:
            break
        dealer_cards.append(deck.pop())
    return deck, dealer_cards


def settle_outcome(
    player_cards: list[str], dealer_cards: list[str], natural: bool
) -> tuple[str, float]:
    """Возвращает `(outcome, multiplier)`. `outcome` — одно из
    `{"natural","win","push","lose","bust"}`. `natural` — вычисленный
    вызывающим флаг: True, если у игрока был натурал на исходной раздаче
    (до любого hit); эта функция сама не пересчитывает натурал у игрока,
    только у дилера (для правила "натурал дилера бьёт любую не-натуральную
    21")."""
    player_value, _ = hand_value(player_cards)
    if player_value > 21:
        return "bust", 0.0

    dealer_natural = is_natural(dealer_cards)

    if natural:
        return ("push", 1.0) if dealer_natural else ("natural", 2.5)

    if dealer_natural:
        # Натурал дилера бьёт любую не-натуральную 21 игрока (в т.ч. после hit).
        return "lose", 0.0

    dealer_value, _ = hand_value(dealer_cards)
    if dealer_value > 21:
        return "win", 2.0
    if player_value > dealer_value:
        return "win", 2.0
    if player_value == dealer_value:
        return "push", 1.0
    return "lose", 0.0
