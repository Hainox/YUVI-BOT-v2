"""Каталог созвездий (constellation bonuses) мира Ювитерии (GACHA-04) —
полная таблица бонусов «созвездий» за дубли каждого из 15 персонажей гачи
(см. `gacha_catalog.py`), ПОЛНОСТЬЮ в коде, не в БД (как и сам каталог
персонажей). Источник данных — дизайн-документ Yuviteria Codex: все 90
узлов (15 героинь x C1..C6) перенесены из него ДОСЛОВНО, без округления и
«причёсывания» значений.

Уровень созвездия персонажа — `const_level(copies)` = `min(6, copies-1)`,
т.е. напрямую переиспользует уже НЕограниченное сверху поле
`GachaCollection.copies` (инкрементируется на каждый дубль в
`gacha_service.py`, см. его докстринг про `row.copies += 1`) — отдельная
миграция БД под уровень созвездия не нужна, новый столбец/таблица не
заводится.

Бонусы одного персонажа СТЕКАЮТСЯ по мере роста уровня (см. `sum_effect`
ниже): узел C4 не заменяет C1..C3 того же персонажа с тем же `effect`, а
складывается с ними. `bonuses_for` возвращает все разблокированные узлы
персонажа — для отображения в UI (codex-экран/карточка персонажа).

Особый случай: у `uur_astrea` узел C4 в исходном дизайн-документе — ОДНА
строка на два эффекта сразу («-8% комиссии перевода и биржи»). Здесь она
разложена на ДВЕ отдельных записи `ConstellationBonus` (обе level=4, одна
на `TRANSFER_FEE_PCT`, другая на `EXCHANGE_FEE_PCT`), поэтому в каталоге
91 запись на 90 дизайнерских узлов (15 героинь x 6 уровней).

ВАЖНО про scope этого модуля: это ЧИСТЫЕ ДАННЫЕ — без побочных эффектов и
без импортов кроме `dataclasses`/`typing` (никакого обращения к БД или к
другим сервисам), сам по себе он НИКУДА не подключён. Из всех effect'ов
ниже только `FARM_CP_PCT` имеет готовую точку подключения к живой игровой
логике (`bot/services/clicker_service.py:_collection_income_per_sec`,
GACHA-02) и является единственным кандидатом на подключение следующим
шагом — собственно подключение (правка `clicker_service.py`) в этот
коммит НЕ входит. Все остальные effect'ы (комиссии перевода/биржи,
дуэли, казино, теги, магазин, gacha pity/cost/banner-weight, daily
freebies) хранятся здесь как полные, реальные, готовые к подключению
данные, но НЕ применяются НИГДЕ в кодовой базе. Подключение каждой
системы намеренно отложено на отдельную последующую задачу per-система:
каждая затрагивает код движения реальных денег со своим контрактом
идемпотентности и порядка блокировок (см. `economy_service.py`,
Pattern 1 — порядок блокировок, ref_id-идемпотентность), который требует
индивидуального ревью. Это задокументированное сознательное решение о
scope, а не недосмотр.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ConstellationBonus:
    """Один узел созвездия персонажа. `value=None` — узел без числового
    эффекта (спец-механика вроде daily freebie или чисто UX-подсказка);
    для таких узлов сумма в `sum_effect` не считается — там нечего
    складывать."""

    char_id: str
    level: int
    effect: str
    value: float | None
    text: str


FARM_CP_PCT = "farm_cp_pct"
FARM_UPGRADE_COST_PCT = "farm_upgrade_cost_pct"
FARM_AUTO_EFFICIENCY_PCT = "farm_auto_efficiency_pct"
FARM_TAP_BONUS = "farm_tap_bonus"
TRANSFER_FEE_PCT = "transfer_fee_pct"
EXCHANGE_FEE_PCT = "exchange_fee_pct"
DUEL_LOSS_REFUND_PCT = "duel_loss_refund_pct"
DUEL_STAKE_BONUS_PCT = "duel_stake_bonus_pct"
DUEL_WIN_PAYOUT_PCT = "duel_win_payout_pct"
MARKET_POOL_SHARE_PCT = "market_pool_share_pct"
CASINO_MULTIPLIER_PCT = "casino_multiplier_pct"
CASINO_CHANCE_PCT = "casino_chance_pct"
TAG_DISCOUNT_PCT = "tag_discount_pct"
SHOP_DISCOUNT_PCT = "shop_discount_pct"
AI_ORDER_DISCOUNT_PCT = "ai_order_discount_pct"
GACHA_PITY_BONUS = "gacha_pity_bonus"
GACHA_COST_DISCOUNT_PCT = "gacha_cost_discount_pct"
GACHA_BANNER_WEIGHT_PCT = "gacha_banner_weight_pct"
DAILY_FREEBIE = "daily_freebie"
UX_ONLY = "ux_only"


CONSTELLATIONS: tuple[ConstellationBonus, ...] = (
    # --- Элис (R) ---
    ConstellationBonus("r_elis", 1, FARM_CP_PCT, 0.02, "+2% CP/сек фермы, если героиня назначена"),
    ConstellationBonus("r_elis", 2, TRANSFER_FEE_PCT, 0.03, "-3% комиссии перевода"),
    ConstellationBonus("r_elis", 3, DUEL_LOSS_REFUND_PCT, 0.03, "Дуэль: частичный возврат ставки при поражении (3%)"),
    ConstellationBonus("r_elis", 4, SHOP_DISCOUNT_PCT, 0.10, "Магазин: «Обнять» дешевле на 10%"),
    ConstellationBonus("r_elis", 5, FARM_UPGRADE_COST_PCT, 0.05, "-5% к стоимости апгрейда фермы"),
    ConstellationBonus("r_elis", 6, DAILY_FREEBIE, None, "«Клятва новобранца» - раз в день +100 ювиков на баланс бесплатно"),

    # --- Фрея (R) ---
    ConstellationBonus("r_freya", 1, FARM_CP_PCT, 0.02, "+2% CP/сек фермы"),
    ConstellationBonus("r_freya", 2, FARM_UPGRADE_COST_PCT, 0.05, "-5% к стоимости апгрейда тапа"),
    ConstellationBonus("r_freya", 3, SHOP_DISCOUNT_PCT, 0.10, "Магазин: «Анекдот» дешевле на 10%"),
    ConstellationBonus("r_freya", 4, DUEL_STAKE_BONUS_PCT, 0.02, "Дуэль: +2% к ставке без доп. риска"),
    ConstellationBonus("r_freya", 5, TRANSFER_FEE_PCT, 0.03, "-3% комиссии перевода"),
    ConstellationBonus("r_freya", 6, DAILY_FREEBIE, None, "«Наковальня» - раз в день следующий апгрейд фермы дешевле на 50%"),

    # --- Селин (R) ---
    ConstellationBonus("r_selin", 1, TRANSFER_FEE_PCT, 0.03, "-3% комиссии перевода"),
    ConstellationBonus("r_selin", 2, EXCHANGE_FEE_PCT, 0.03, "-3% комиссии биржи"),
    ConstellationBonus("r_selin", 3, MARKET_POOL_SHARE_PCT, 0.02, "Рынки: +2% к доле пула при ставке"),
    ConstellationBonus("r_selin", 4, UX_ONLY, None, "Гача: коллекция подсвечивает недостающих персонажей (UX)"),
    ConstellationBonus("r_selin", 5, EXCHANGE_FEE_PCT, 0.05, "-5% комиссии биржи"),
    ConstellationBonus("r_selin", 6, DAILY_FREEBIE, None, "«Дальний путь» - раз в день один перевод без комиссии"),

    # --- София (R) ---
    ConstellationBonus("r_sofia", 1, FARM_CP_PCT, 0.02, "+2% CP/сек фермы"),
    ConstellationBonus("r_sofia", 2, DUEL_LOSS_REFUND_PCT, 0.03, "Дуэль: +3% возврата ставки при поражении"),
    ConstellationBonus("r_sofia", 3, TAG_DISCOUNT_PCT, 0.10, "Теги: -10% к цене аренды"),
    ConstellationBonus("r_sofia", 4, SHOP_DISCOUNT_PCT, 0.10, "Магазин: «Пнуть» дешевле на 10%"),
    ConstellationBonus("r_sofia", 5, DUEL_LOSS_REFUND_PCT, 0.05, "Дуэль: +5% возврата (стак с C2)"),
    ConstellationBonus("r_sofia", 6, DAILY_FREEBIE, None, "«Городская стража» - раз в день первый проигрыш в дуэли не списывает ставку"),

    # --- Нора (R) ---
    ConstellationBonus("r_nora", 1, FARM_CP_PCT, 0.02, "+2% CP/сек фермы"),
    ConstellationBonus("r_nora", 2, SHOP_DISCOUNT_PCT, 0.10, "Магазин: «Прожарка» дешевле на 10%"),
    ConstellationBonus("r_nora", 3, GACHA_PITY_BONUS, None, "Гача: +1 к пити UR за каждый x10-ролл"),
    ConstellationBonus("r_nora", 4, TRANSFER_FEE_PCT, 0.03, "-3% комиссии перевода"),
    ConstellationBonus("r_nora", 5, DUEL_WIN_PAYOUT_PCT, 0.05, "Дуэль: +5% к выплате при победе"),
    ConstellationBonus("r_nora", 6, DAILY_FREEBIE, None, "«Меткий выстрел» - раз в день +150 ювиков на баланс бесплатно"),

    # --- Игнис (S) ---
    ConstellationBonus("s_ignis", 1, FARM_CP_PCT, 0.03, "+3% CP/сек фермы"),
    ConstellationBonus("s_ignis", 2, CASINO_CHANCE_PCT, 0.03, "Слоты: +3% к шансу фриспина"),
    ConstellationBonus("s_ignis", 3, DUEL_STAKE_BONUS_PCT, 0.05, "Дуэль: +5% к ставке без доп. риска"),
    ConstellationBonus("s_ignis", 4, FARM_UPGRADE_COST_PCT, 0.05, "-5% к стоимости апгрейда автокликера"),
    ConstellationBonus("s_ignis", 5, CASINO_MULTIPLIER_PCT, 0.05, "Слоты: +5% к множителю выигрыша"),
    ConstellationBonus("s_ignis", 6, DAILY_FREEBIE, None, "«Клеймо пламени» - раз в день следующий спин в слотах бесплатный"),

    # --- Астрид (S) ---
    ConstellationBonus("s_astrid", 1, FARM_CP_PCT, 0.03, "+3% CP/сек фермы"),
    ConstellationBonus("s_astrid", 2, CASINO_CHANCE_PCT, 0.03, "Рулетка/кости: +3% к шансу «точного» исхода"),
    ConstellationBonus("s_astrid", 3, TRANSFER_FEE_PCT, 0.05, "-5% комиссии перевода"),
    ConstellationBonus("s_astrid", 4, GACHA_PITY_BONUS, None, "Гача: +1 к пити UUR за каждый x10-ролл"),
    ConstellationBonus("s_astrid", 5, DUEL_WIN_PAYOUT_PCT, 0.05, "Дуэль: +5% к выплате при победе"),
    ConstellationBonus("s_astrid", 6, DAILY_FREEBIE, None, "«Ледяной прицел» - раз в день гарантированный выигрышный бросок костей"),

    # --- Амира (S) ---
    ConstellationBonus("s_amira", 1, UX_ONLY, None, "Гача: точный шанс тира виден до ролла (UX)"),
    ConstellationBonus("s_amira", 2, FARM_CP_PCT, 0.03, "+3% CP/сек фермы"),
    ConstellationBonus("s_amira", 3, EXCHANGE_FEE_PCT, 0.05, "-5% комиссии биржи"),
    ConstellationBonus("s_amira", 4, MARKET_POOL_SHARE_PCT, 0.03, "Рынки: +3% к доле пула"),
    ConstellationBonus("s_amira", 5, AI_ORDER_DISCOUNT_PCT, 0.15, "AI-заказы (анекдот/роаст/фидбек): -15%"),
    ConstellationBonus("s_amira", 6, UX_ONLY, None, "«Гадание Амиры» - раз в день подсказка тира следующего ролла (только предсказание, исход не меняет)"),

    # --- Луна (S) ---
    ConstellationBonus("s_luna", 1, FARM_CP_PCT, 0.03, "+3% CP/сек фермы"),
    ConstellationBonus("s_luna", 2, SHOP_DISCOUNT_PCT, 0.05, "Магазин: любое действие -5%"),
    ConstellationBonus("s_luna", 3, CASINO_CHANCE_PCT, 0.03, "Блэкджек/кости: +3% к «удаче» исхода"),
    ConstellationBonus("s_luna", 4, TRANSFER_FEE_PCT, 0.05, "-5% комиссии перевода"),
    ConstellationBonus("s_luna", 5, GACHA_COST_DISCOUNT_PCT, 0.03, "-3% к стоимости x10-ролла"),
    ConstellationBonus("s_luna", 6, DAILY_FREEBIE, None, "«Нестабильный эксперимент» - раз в день случайный бесплатный бонус (ролл/спин/+200 ювиков)"),

    # --- Айрис (UR) ---
    ConstellationBonus("ur_iris", 1, FARM_CP_PCT, 0.04, "+4% CP/сек фермы"),
    ConstellationBonus("ur_iris", 2, DUEL_STAKE_BONUS_PCT, 0.08, "Дуэль: +8% к ставке без доп. риска"),
    ConstellationBonus("ur_iris", 3, TRANSFER_FEE_PCT, 0.05, "-5% комиссии перевода"),
    ConstellationBonus("ur_iris", 4, TAG_DISCOUNT_PCT, 0.15, "Теги: -15% к цене аренды"),
    ConstellationBonus("ur_iris", 5, DUEL_WIN_PAYOUT_PCT, 0.08, "Дуэль: +8% к выплате при победе"),
    ConstellationBonus("ur_iris", 6, DAILY_FREEBIE, None, "«Приказ Лорда-протектора» - раз в день гарантированная победа в дуэли со ставкой <=500 ювиков"),

    # --- Юна (UR) ---
    ConstellationBonus("ur_yuna", 1, FARM_CP_PCT, 0.04, "+4% CP/сек фермы"),
    ConstellationBonus("ur_yuna", 2, FARM_TAP_BONUS, None, "Ферма: раз в 10 тапов - доп. +1 CP («рывок»)"),
    ConstellationBonus("ur_yuna", 3, EXCHANGE_FEE_PCT, 0.05, "-5% комиссии биржи"),
    ConstellationBonus("ur_yuna", 4, CASINO_MULTIPLIER_PCT, 0.05, "Слоты: +5% к множителю выигрыша"),
    ConstellationBonus("ur_yuna", 5, GACHA_COST_DISCOUNT_PCT, 0.03, "-3% к стоимости роллов гачи"),
    ConstellationBonus("ur_yuna", 6, DAILY_FREEBIE, None, "«Лисий рывок» - раз в день бесплатный x10-ролл"),

    # --- Мия (UR) ---
    ConstellationBonus("ur_mia", 1, FARM_CP_PCT, 0.04, "+4% CP/сек фермы"),
    ConstellationBonus("ur_mia", 2, FARM_AUTO_EFFICIENCY_PCT, 0.05, "Автокликер фермы: +5% эффективности"),
    ConstellationBonus("ur_mia", 3, CASINO_CHANCE_PCT, 0.05, "Кости: +5% к шансу удачного исхода"),
    ConstellationBonus("ur_mia", 4, TRANSFER_FEE_PCT, 0.05, "-5% комиссии перевода"),
    ConstellationBonus("ur_mia", 5, DUEL_WIN_PAYOUT_PCT, 0.08, "Дуэль: +8% к выплате при победе"),
    ConstellationBonus("ur_mia", 6, DAILY_FREEBIE, None, "«Молниеносный бросок» - раз в день гарантированный фриспин в слотах"),

    # --- Астрея (UUR) ---
    ConstellationBonus("uur_astrea", 1, GACHA_COST_DISCOUNT_PCT, 0.05, "-5% к стоимости всех роллов гачи"),
    ConstellationBonus("uur_astrea", 2, FARM_CP_PCT, 0.06, "+6% CP/сек фермы"),
    ConstellationBonus("uur_astrea", 3, GACHA_PITY_BONUS, None, "Пити UR и UUR растут на +1 быстрее с КАЖДОГО ролла (не только x10)"),
    # C4 в дизайн-документе — одна строка на два эффекта сразу ("-8%
    # комиссии перевода и биржи"), разложена здесь на две отдельные записи
    # (см. докстринг модуля) — потому у uur_astrea 7 записей, а не 6.
    ConstellationBonus("uur_astrea", 4, TRANSFER_FEE_PCT, 0.08, "-8% комиссии перевода"),
    ConstellationBonus("uur_astrea", 4, EXCHANGE_FEE_PCT, 0.08, "-8% комиссии биржи"),
    ConstellationBonus("uur_astrea", 5, DUEL_LOSS_REFUND_PCT, 0.15, "Дуэль: страховка - возврат 15% ставки при поражении"),
    ConstellationBonus("uur_astrea", 6, DAILY_FREEBIE, None, "«Отказ от партии» - раз в день можно отменить любой исход (ролл/дуэль/казино) до подтверждения и переиграть бесплатно"),

    # --- Элиана (UUR) ---
    ConstellationBonus("uur_eliana", 1, FARM_CP_PCT, 0.06, "+6% CP/сек фермы"),
    ConstellationBonus("uur_eliana", 2, DUEL_LOSS_REFUND_PCT, 0.20, "Дуэль: страховка - возврат 20% ставки при поражении"),
    ConstellationBonus("uur_eliana", 3, TRANSFER_FEE_PCT, 0.08, "-8% комиссии перевода"),
    ConstellationBonus("uur_eliana", 4, SHOP_DISCOUNT_PCT, 0.15, "Магазин: все действия -15%"),
    ConstellationBonus("uur_eliana", 5, TAG_DISCOUNT_PCT, 0.20, "Теги: -20% к цене аренды"),
    ConstellationBonus("uur_eliana", 6, DAILY_FREEBIE, None, "«Благословение полуангела» - раз в день полный возврат ставки при любом проигрыше (казино/дуэль/рынок)"),

    # --- Мара (UUR) ---
    ConstellationBonus("uur_mara", 1, FARM_CP_PCT, 0.06, "+6% CP/сек фермы"),
    ConstellationBonus("uur_mara", 2, CASINO_MULTIPLIER_PCT, 0.08, "Казино: +8% к множителю выигрыша (без страховки - высокий риск/награда)"),
    ConstellationBonus("uur_mara", 3, DUEL_STAKE_BONUS_PCT, 0.12, "Дуэль: +12% к ставке без доп. риска"),
    ConstellationBonus("uur_mara", 4, GACHA_BANNER_WEIGHT_PCT, 0.05, "Гача: rate-up баннер получает +5% к весу среди UUR"),
    ConstellationBonus("uur_mara", 5, EXCHANGE_FEE_PCT, 0.08, "-8% комиссии биржи"),
    ConstellationBonus("uur_mara", 6, DAILY_FREEBIE, None, "«Сделка с Бездной» - раз в день удвой любую ставку (казино/дуэль): победа x2, поражение - обычная потеря"),
)


def const_level(copies: int) -> int:
    """min(6, copies-1), floored at 0. 1-я копия = C0 (без бонуса)."""
    return max(0, min(6, copies - 1))


def sum_effect(char_id: str, level: int, effect: str) -> float:
    """Сумма value всех ConstellationBonus этого персонажа, чей уровень <= level
    и чей effect совпадает — бонусы СТЕКАЮТСЯ по мере роста уровня созвездия
    (не только последний разблокированный узел действует), см. модульный докстринг.
    None-value записи (спец-механики/UX) игнорируются."""
    return sum(
        c.value for c in CONSTELLATIONS
        if c.char_id == char_id and c.level <= level and c.effect == effect and c.value is not None
    )


def bonuses_for(char_id: str, level: int) -> list[ConstellationBonus]:
    """Все разблокированные бонусы персонажа (level 1..level включительно),
    для отображения в UI (codex-экран/карточка персонажа)."""
    return sorted(
        (c for c in CONSTELLATIONS if c.char_id == char_id and c.level <= level),
        key=lambda c: c.level,
    )
