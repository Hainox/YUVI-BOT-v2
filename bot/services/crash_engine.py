"""Чистая (без DB/session) логика игры "Crash" — генерация скрытой точки
краша, живой множитель по прошедшему wall-clock времени, обратная функция
для таймаут-резолвера. По образцу `blackjack_engine.py` — никаких побочных
эффектов и никакого собственного RNG (энтропия приходит через переданный
`rng`, вызывающий передаёт `casino_service._rng`, `secrets.SystemRandom()`).

Провably-fair распределение точки краша (стандартная схема crash-игр):
с вероятностью `CRASH_HOUSE_EDGE` раунд лопается мгновенно на 1.00x (никто
не может выиграть такой раунд — кэшаут в самый первый момент даёт
`multiplier_at(0) == 1.00`, а `1.00 < 1.00` ложно, т.е. это проигрыш).
Иначе `crash_point = (1-edge)/(1-r)`, `r` равномерно на `[edge, 1)` — в
непрерывной (не квантизованной до центов) математике эта формула держит RTP
РОВНО `1-CRASH_HOUSE_EDGE` для ЛЮБОЙ стратегии "всегда кэшаутиться на X"
(E[payout] = X * P(crash_point>=X) = X * (1-edge)/X = 1-edge, независимо от X).

Аудит-находка (2026-08-06): на практике реальный RTP чуть ниже — измерено
~97.5-97.7% вместо номинальных 98%. Причина — `ROUND_DOWN`-квантизация,
применённая ДВАЖДЫ (сама точка краша floor'ится здесь, а живой множитель на
кэшауте floor'ится ещё раз в `multiplier_at`), и обе округления складываются
строго в пользу банка (никогда в пользу игрока) — это НЕ баг безопасности
(не эксплойт, не источник переплаты), а известное, измеренное расхождение
между идеальной непрерывной моделью и её дискретной (до цента множителя)
реализацией. Если где-то заявляется точный "2% house edge" — фактический
чуть выше, около 2.3-2.5%.

D-06-класса аудит-урок дайса (2026-08-06, см. `casino_service.dice_fair_payout`
докстринг): float-арифметика в формуле выплаты копит погрешность округления и
на части входов расходится с точным результатом. Здесь то же самое
недопустимо для суммы, определяющей исход всего раунда, поэтому ВСЯ цепочка
"точка краша -> прошедшее время -> текущий множитель -> выплата" — это
`decimal.Decimal` от первого до последнего шага. Единственное место, где
касается float, — `rng.random()` (сырая энтропия по построению
`secrets.SystemRandom`), и он немедленно оборачивается в `Decimal(...)`
прежде, чем участвовать хоть в одном вычислении; float после этой строчки
в payout-путь больше никогда не попадает.

`generate_crash_point`/`multiplier_at` floor'ят (`ROUND_DOWN`) итог до 2
знаков — хранимое/используемое значение всегда точное и воспроизводимое, а
не "сырой" бесконечно-точный результат деления/`.exp()`."""

from __future__ import annotations

from decimal import Decimal
from decimal import ROUND_DOWN

# 2% house edge — тот же формат константы (Decimal, не float), что
# CRASH-специфичный контекст задаёт, но по духу — DICE_HOUSE_EDGE (casino_service.py).
CRASH_HOUSE_EDGE = Decimal("0.02")

# Хардкап множителя (страховка от вырожденных r->1, где (1-edge)/(1-r) улетает
# в произвольно большое число) — по духу FREESPINS_HARD_CAP в teto_slot_engine.
CRASH_MAX_MULTIPLIER = Decimal("100.00")

# Темп роста e^(rate*t): ln(2)/0.173 ≈ 4.0066с до 2x — комфортный темп для
# чат-мини-аппа (не мгновенно, но и не полминуты ожидания одного удвоения).
CRASH_GROWTH_RATE = Decimal("0.173")

_TWO_PLACES = Decimal("0.01")


def generate_crash_point(rng) -> Decimal:
    """Тянет точку краша раунда (server-authoritative, D-03/T-04.1-01 —
    клиент никогда не поставляет исход). `rng` — `secrets.SystemRandom()`-
    совместимый объект (передан вызывающим, эта функция сама RNG не создаёт).

    С вероятностью `CRASH_HOUSE_EDGE` — мгновенный краш на 1.00x. Иначе —
    `(1-edge)/(1-r)`, floor'нутое до 2 знаков (`ROUND_DOWN`) и урезанное
    `CRASH_MAX_MULTIPLIER` сверху. Возвращает `Decimal`, никогда `float`."""
    r = Decimal(rng.random())
    if r < CRASH_HOUSE_EDGE:
        return Decimal("1.00")
    point = (Decimal(1) - CRASH_HOUSE_EDGE) / (Decimal(1) - r)
    point = point.quantize(_TWO_PLACES, rounding=ROUND_DOWN)
    return min(point, CRASH_MAX_MULTIPLIER)


def multiplier_at(elapsed_seconds: Decimal) -> Decimal:
    """Живой множитель раунда через `elapsed_seconds` (`Decimal`, >= 0) после
    старта: `e^(CRASH_GROWTH_RATE * elapsed_seconds)`, floor'нутое до 2 знаков
    той же дисциплиной, что `generate_crash_point` (payout всегда считается
    от точного значения, не от сырого неограниченно-точного `.exp()`).
    `Decimal.exp()` — тот же нативный метод, что `clicker_service.amm_tick`
    использует для точной log/exp-арифметики (см. его докстринг)."""
    if elapsed_seconds < 0:
        raise ValueError("elapsed_seconds должен быть >= 0")
    raw = (CRASH_GROWTH_RATE * elapsed_seconds).exp()
    # Аудит-находка: raw неограничен (для elapsed_seconds порядка нескольких
    # минут — если раунд завис активным дольше, чем таймаут-джоба успела его
    # снять, см. casino_service.resolve_crash_timeouts — .exp() легко даёт
    # число на 25+ значащих цифр), а .quantize() ниже требует, чтобы итог
    # укладывался в точность контекста Decimal (28 знаков по умолчанию) —
    # без капа здесь .quantize() падает с decimal.InvalidOperation вместо
    # тихого возврата большого числа. Капаем ДО quantize той же константой,
    # что и generate_crash_point выше — единственный способ гарантировать,
    # что quantize всегда получает значение, которое реально помещается.
    capped = min(raw, CRASH_MAX_MULTIPLIER)
    return capped.quantize(_TWO_PLACES, rounding=ROUND_DOWN)


def seconds_to_reach(target_multiplier: Decimal) -> Decimal:
    """Обратная функция `multiplier_at`: сколько секунд после старта раунда
    нужно, чтобы множитель достиг `target_multiplier` — `ln(target)/rate`.
    Нужна ТОЛЬКО таймаут-джобу (`casino_service.resolve_crash_timeouts`),
    чтобы понять, что реальное время уже точно прошло точку краша активного
    раунда, без единого клиентского репорта о прогрессе раунда."""
    return target_multiplier.ln() / CRASH_GROWTH_RATE
