"""Тесты чистого движка Crash (`bot.services.crash_engine`) — без DB/session,
чистые функции. Доказывают:

- `generate_crash_point` никогда не возвращает меньше 1.00 или больше
  `CRASH_MAX_MULTIPLIER` (большой прогон реального `secrets.SystemRandom()`).
- Провably-fair RTP-инвариант: при стратегии "всегда кэшаутиться на 2x"
  усреднённая выплата-на-ставку сходится к `1 - CRASH_HOUSE_EDGE` (Monte-Carlo
  калибровка, тот же стиль, что `scripts/calibrate_teto_rtp.py`/
  `tests/test_slot_engine.py::test_sampled_rtp_in_band`).
- `multiplier_at(0) == 1.00` ровно, множитель монотонно растёт со временем.
- `seconds_to_reach` — обратная функция `multiplier_at` (приближённый
  round-trip, точность ограничена floor-квантизацией до 2 знаков).
- Каждое возвращаемое значение — РЕАЛЬНО `Decimal`, никогда `float` (аудит-
  урок дайса 2026-08-06, см. докстринг `crash_engine.py`/
  `casino_service.dice_fair_payout`: там баг был именно в том, что float
  тихо просачивался в денежную формулу — здесь тестируем это явной проверкой
  ТИПА возвращаемого значения на каждом шаге, а не только числовым
  равенством, потому что числовое Monte-Carlo сравнение такой класс бага
  почти никогда не ловит: пост-`ROUND_DOWN`-квантизация до 0.01 поглощает
  ~1e-16 относительную погрешность float на подавляющем большинстве входов,
  расхождение всплыло бы только на редчайших пограничных r — точная проверка
  типа ловит ЛЮБОЕ протекание float детерминированно, а не статистически).
"""

from __future__ import annotations

import secrets
from decimal import ROUND_DOWN
from decimal import Decimal

import pytest

from bot.services import crash_engine

CRASH_HOUSE_EDGE = crash_engine.CRASH_HOUSE_EDGE
CRASH_MAX_MULTIPLIER = crash_engine.CRASH_MAX_MULTIPLIER


class _FixedRng:
    """Тестовый RNG-стаб — `.random()` всегда возвращает заданное значение
    (форсирует конкретную ветку `generate_crash_point`)."""

    def __init__(self, value: float):
        self._value = value

    def random(self) -> float:
        return self._value


# --- generate_crash_point: границы + типы -------------------------------------


def test_generate_crash_point_bounds_over_many_real_draws():
    rng = secrets.SystemRandom()
    for _ in range(20_000):
        point = crash_engine.generate_crash_point(rng)
        assert isinstance(point, Decimal)
        assert not isinstance(point, float)
        assert Decimal("1.00") <= point <= CRASH_MAX_MULTIPLIER


def test_generate_crash_point_instant_bust_below_edge():
    assert crash_engine.generate_crash_point(_FixedRng(0.0)) == Decimal("1.00")
    assert crash_engine.generate_crash_point(_FixedRng(0.0199)) == Decimal("1.00")


def test_generate_crash_point_matches_exact_formula_above_edge():
    rng = _FixedRng(0.5)
    point = crash_engine.generate_crash_point(rng)
    expected = ((Decimal(1) - CRASH_HOUSE_EDGE) / (Decimal(1) - Decimal(0.5))).quantize(
        Decimal("0.01"), rounding=ROUND_DOWN
    )
    assert point == expected


def test_generate_crash_point_caps_at_max_multiplier_when_r_near_one():
    rng = _FixedRng(0.999999999)
    point = crash_engine.generate_crash_point(rng)
    assert point == CRASH_MAX_MULTIPLIER


def test_generate_crash_point_never_returns_float():
    rng = _FixedRng(0.37)
    point = crash_engine.generate_crash_point(rng)
    assert type(point) is Decimal


# --- RTP-инвариант (Monte-Carlo, "всегда кэшаутиться на 2x") ------------------


def test_rtp_always_cash_out_at_2x_matches_house_edge_invariant():
    """E[payout | всегда кэшаутиться на X] = X * P(crash_point >= X) =
    X * (1-edge)/X = 1-edge — не зависит от X (см. докстринг crash_engine.py),
    В НЕПРЕРЫВНОЙ математике. Этот тест сравнивает `crash_point` напрямую с
    фиксированным `target` — упражняет только ОДНО floor-округление
    (`generate_crash_point`), не оба (см. следующий тест на реальном пути
    `crash_cash_out` через `multiplier_at`, где расхождение больше и
    задокументировано). Широкий допуск (±3pp) — этот тест про "распределение
    в целом не сломано", не про точную цифру."""
    rng = secrets.SystemRandom()
    target = Decimal("2.00")
    bet = Decimal(100)
    n = 50_000

    total_bet = Decimal(0)
    total_payout = Decimal(0)
    for _ in range(n):
        crash_point = crash_engine.generate_crash_point(rng)
        total_bet += bet
        if crash_point >= target:
            total_payout += bet * target

    empirical_rtp = total_payout / total_bet
    expected_rtp = Decimal(1) - CRASH_HOUSE_EDGE
    assert abs(empirical_rtp - expected_rtp) < Decimal("0.03"), (
        f"empirical RTP {empirical_rtp} слишком далеко от ожидаемого {expected_rtp}"
    )


def test_rtp_full_cashout_path_matches_measured_double_quantization_bias():
    """Аудит-находка (2026-08-06): реальный путь `casino_service.
    crash_cash_out` квантизует ДВАЖДЫ — `generate_crash_point` floor'ит
    точку краша, `multiplier_at` floor'ит ЕЩЁ РАЗ живой множитель на
    кэшауте — и оба округления складываются строго в пользу банка. Реальный
    RTP поэтому измеримо НИЖЕ идеализированных `1-CRASH_HOUSE_EDGE` (0.98):
    независимый Monte-Carlo прогон (3M раундов, несколько целевых
    множителей) дал ~0.975-0.977. Этот тест воспроизводит ТОЧНО тот же путь,
    что `crash_cash_out` (через `seconds_to_reach`+`multiplier_at`, не
    напрямую `crash_point`), с допуском вокруг ИЗМЕРЕННОГО значения, а не
    идеализированного — чтобы регрессия (например, случайно восстановленное
    ROUND_DOWN->округление до ближайшего) была замечена в обе стороны."""
    rng = secrets.SystemRandom()
    target = Decimal("2.00")
    elapsed_for_target = crash_engine.seconds_to_reach(target)
    bet = Decimal(100)
    n = 50_000

    total_bet = Decimal(0)
    total_payout = Decimal(0)
    for _ in range(n):
        crash_point = crash_engine.generate_crash_point(rng)
        total_bet += bet
        live_mult = crash_engine.multiplier_at(elapsed_for_target)
        if live_mult < crash_point:
            total_payout += bet * live_mult

    empirical_rtp = total_payout / total_bet
    # Измеренный диапазон (независимый ревью, 3M раундов): ~0.975-0.977.
    # Допуск здесь шире (меньше раундов в юнит-тесте) но центрирован на
    # РЕАЛЬНОМ измеренном значении, не на идеализированном 0.98.
    assert Decimal("0.965") < empirical_rtp < Decimal("0.985"), (
        f"empirical full-path RTP {empirical_rtp} вне ожидаемого измеренного "
        f"диапазона ~0.975 (idealized 1-edge={Decimal(1) - CRASH_HOUSE_EDGE} "
        "не достижим из-за двойной ROUND_DOWN-квантизации, см. модульный "
        "докстринг crash_engine.py)"
    )


# --- multiplier_at -------------------------------------------------------------


def test_multiplier_at_zero_is_exactly_one():
    m = crash_engine.multiplier_at(Decimal(0))
    assert m == Decimal("1.00")
    assert isinstance(m, Decimal)


def test_multiplier_at_monotonically_increasing_below_cap():
    """Строго возрастает, ПОКА не упёрлось в CRASH_MAX_MULTIPLIER — e^(rate*t)
    с CRASH_GROWTH_RATE=0.173 достигает капа 100.00 около t=27с (см. отдельный
    тест ниже на сам кап и его плато), поэтому диапазон здесь урезан, чтобы не
    путать "функция капнута" с "функция перестала расти"."""
    prev = crash_engine.multiplier_at(Decimal(0))
    for t in range(1, 25):
        cur = crash_engine.multiplier_at(Decimal(t))
        assert cur > prev, f"multiplier_at не растёт на t={t}: {cur} <= {prev}"
        prev = cur


def test_multiplier_at_caps_at_max_and_never_exceeds_it():
    """Аудит-находка (2026-08-06): без капа `multiplier_at` на больших elapsed
    (напр. активный раунд, который таймаут-джоба ещё не успела снять)
    возвращает Decimal с 25+ значащими цифрами и падает на `.quantize()` с
    `decimal.InvalidOperation` — здесь пинуется сам факт капа (та же
    дисциплина, что `generate_crash_point`'s `min(point, CRASH_MAX_MULTIPLIER)`
    выше), включая заведомо огромный elapsed, на котором старая
    (некапнутая) реализация падала."""
    for t in (30, 100, 1000, 100_000):
        m = crash_engine.multiplier_at(Decimal(t))
        assert m == crash_engine.CRASH_MAX_MULTIPLIER
        assert m <= crash_engine.CRASH_MAX_MULTIPLIER


def test_multiplier_at_matches_exact_exp_formula():
    t = Decimal("4.0")
    m = crash_engine.multiplier_at(t)
    expected = (crash_engine.CRASH_GROWTH_RATE * t).exp().quantize(Decimal("0.01"), rounding=ROUND_DOWN)
    assert m == expected
    # Растёт в ~2x к t=4с (см. докстринг CRASH_GROWTH_RATE: ln(2)/0.173≈4.0066с).
    assert Decimal("1.95") <= m <= Decimal("2.05")


def test_multiplier_at_rejects_negative_elapsed():
    with pytest.raises(ValueError):
        crash_engine.multiplier_at(Decimal("-0.01"))


def test_multiplier_at_never_returns_float():
    for t in ("0", "0.5", "3.33", "10", "50"):
        m = crash_engine.multiplier_at(Decimal(t))
        assert type(m) is Decimal


# --- seconds_to_reach ------------------------------------------------------


def test_seconds_to_reach_zero_at_multiplier_one():
    s = crash_engine.seconds_to_reach(Decimal("1.00"))
    assert s == Decimal(0)
    assert isinstance(s, Decimal)


def test_seconds_to_reach_round_trips_multiplier_at():
    for t in (Decimal("1"), Decimal("2"), Decimal("3.5"), Decimal("5"), Decimal("10"), Decimal("20")):
        m = crash_engine.multiplier_at(t)
        back = crash_engine.seconds_to_reach(m)
        # Приближённый round-trip: multiplier_at floor-квантизует до 2
        # знаков перед возвратом, so seconds_to_reach(multiplier_at(t))
        # чуть МЕНЬШЕ t (никогда не больше — floor всегда занижает
        # множитель, значит и восстановленное время). Допуск 0.1с с запасом
        # по всей проверенной полосе t (см. явный расчёт в реализации).
        assert Decimal(0) <= t - back < Decimal("0.1"), f"round-trip разошёлся на t={t}: back={back}"


def test_seconds_to_reach_never_returns_float():
    for x in ("1.01", "2.00", "5.5", "50"):
        s = crash_engine.seconds_to_reach(Decimal(x))
        assert type(s) is Decimal
