"""POST /api/v1/games/coinflip — тонкая обёртка над `casino_service.
play_coinflip` (CASINO-01, T-04.2-01/02/03).

`user_id`/`chat_id` берутся ТОЛЬКО из `AuthContext` (`require_membership`) —
Pydantic-модель тела запроса намеренно НЕ содержит поле `user_id` вовсе:
любое лишнее поле в JSON-теле (например поддельный `user_id` атакующего)
FastAPI/Pydantic молча игнорирует, роут никогда его не читает (IDOR,
T-04.2-02, доказано `tests/test_api_games.py::
test_coinflip_ignores_foreign_user_id_in_body_idor`).

Исключения `casino_service`/`economy_service` маппятся на HTTP:
`InvalidBet`/`InsufficientFunds` -> 400 (T-04.2-03, Pydantic `Field(ge=1)` —
первая линия защиты, `casino_service._validate_bet` — вторая, серверная);
`GameNotActive` -> 409. `DuplicateRound` — гонка конкурентных запросов с
одним `idem_key` (НЕ обычный replay: тот уже гасится внутри `_settle` без
исключения) — один повторный вызов `play_coinflip` обычно застаёт уже
завершённую конкурентную транзакцию и возвращает тот же успешный
сохранённый исход клиенту; если гонка не разрешилась и повторно — 409.

После успешного settle роут дочитывает свежий баланс и публикует его в
`bal:{chat_id}` через `balance_events.publish_balance` (D-02) — до 04.2-02
ни один вызывающий код фактически не звал `publish_balance` (сам примитив
и `GET /api/v1/events` существовали с Фазы 4, но были не подключены ни к
одному денежному пути), из-за чего SSE-канал никогда бы не сработал даже
после реального выигрыша/проигрыша. Ответ также несёт `user_balance_after`,
чтобы `lib/api.ts`'s balance-sniffing на клиенте сработал мгновенно, не
дожидаясь SSE round-trip (см. 04.2-RESEARCH.md `lib/api.ts` Code Example).

`bank_capped` (bool, все три игры этого файла — coinflip/dice/roulette):
D-06 (`economy_service.pay_from_bank`) урезает выплату до текущего остатка
`chat_bank` — на свежем чате с пустым банком выигрыш по факту RNG может
выплатить МЕНЬШЕ честного payout (в худшем случае вплоть до `bet`, т.е.
баланс игрока не меняется вовсе, хотя раунд был выигран). Без явного флага
это выглядит для игрока как "баланс не обновился после победы" (реальный
инцидент верификации 04.2-02: /me до и после раунда — 1000 и 1000, банк
чата был 0, весь выигрыш ушёл на компенсацию собственной же ставки).
Флаг вычисляется ЗДЕСЬ (роут), не в `casino_service.py` — модуль settle-ядра
общий для всех игр казино с разными формулами мультипликатора, трогать его
ради одного UI-флага не нужно. roulette (04.2-03) повторяет тот же паттерн,
что и coinflip (04.2-02), пересчитывая свою "честную" формулу выплаты по
данным, уже доступным роуту (bet_type), а не дублируя приватную
`compute()`-логику `casino_service`. dice — ИСКЛЮЧЕНИЕ из этого правила
(bugfix аудита 2026-08-06): роут раньше держал СВОЮ копию float-формулы
`(1 - DICE_HOUSE_EDGE) / win_prob`, независимо от `casino_service.play_dice`'s
такой же формулы — оба места накапливали одну и ту же float-погрешность
округления по отдельности (см. `casino_service.dice_fair_payout`'s
докстринг), поэтому здесь она НЕ дублируется по формуле, а вызывается ОДНА
общая точная (Fraction, не float) функция `casino_service.dice_fair_payout`
— единственный способ гарантировать, что "честная" сумма здесь и реальный
`payout` из settle всегда считаются identично.

POST /games/dice и POST /games/roulette (04.2-03) — тот же тонкий паттерн,
что и coinflip выше: `casino_service.play_dice`/`play_roulette` уже несут
всю валидацию/RNG/settle-логику (04.1-01), роут только парсит тело,
прокидывает `auth.user_id`/`auth.chat_id`, маппит исключения на HTTP и
публикует баланс.

POST /games/slots (04.2-10) — тот же тонкий стейтлес-паттерн, что и
coinflip/dice/roulette: `casino_service.play_slots` (04.1-02) уже несёт всю
RNG/paytable/фриспин-логику, роут только прокидывает `bet`/`idem_key`.
`bank_capped` НЕ вычисляется для слотов (в отличие от coinflip/dice/roulette
выше) — в отличие от их фиксированной формулы множителя, "честная" выплата
слота зависит от конкретной выпавшей сетки (линии + доигранные фриспины) и
известна ТОЛЬКО внутри `casino_service.play_slots.compute()` до капа банком;
роут её не видит (`_settle` возвращает наружу только уже капнутый `payout`).
Экспонирование этого числа потребовало бы трогать общее settle-ядро ради
одного UI-флага — то самое, чего докстринг `_settle` явно просит избегать;
оставлено как осознанный, задокументированный пробел (не тихий баг), не
входящий в must_haves этого плана.

POST /games/teto_slots — тот же тонкий стейтлес-паттерн, что и POST
/games/slots выше: `casino_service.play_teto_slots` уже несёт всю
RNG/мегаблок/каскад/Дрель-Хант/лестница-логику, роут только прокидывает
`bet`/`idem_key` (переиспользует ту же Pydantic-модель `SlotsBet` — форма
тела запроса идентична). БЕЗ джекпот-слоя: `play_teto_slots` не возвращает
ключ `"jackpot"` вовсе (джекпот CASINO-06 по дизайну — фича конкретно
Azumanga, см. докстринг `play_teto_slots`), поэтому роут его и не проверяет.

`bank_capped` У TETO ВЫЧИСЛЯЕТСЯ (в отличие от Azumanga-слотов выше) —
и это НЕ переголосованное решение, а другой набор фактов. Аргумент слотов
("честная выплата известна только внутри `compute()`, роут её не видит") для
Тето просто неверен: движок кладёт неурезанный итог спина в САМ ОТВЕТ, как
`outcome.total_payout`, поэтому роуту нечего восстанавливать — сравнение
`payout < outcome["total_payout"]` использует два числа, которые у него уже
на руках, и не требует трогать общее settle-ядро (та же дисциплина, что и
у coinflip/dice/roulette, где флаг тоже считается ЗДЕСЬ). Считается
БЕЗУСЛОВНО, а не только "если выиграл": у Тето нет отдельного признака
победы, `total_payout == 0` — обычный проигрыш, и `bank_capped: false` в
этом случае — честный ответ, а не пропущенный ключ, который клиенту
пришлось бы отличать от `false`. Есть и на replay (стоит в `outcome`), т.е.
не зависит от наличия анимации.

Почему это вообще важно именно здесь: у Тето один спин легко платит больше
ставки (измерено на 20 000 реальных спинов — 10.8% спинов, максимум 126x
ставки), а `pay_from_bank` (D-06) урежет выплату до остатка банка чата. Без
флага это тот же самый инцидент, что уже был на Azumanga (см. `bank_capped`
выше: "/me до и после раунда — 1000 и 1000, банк чата был 0"), только
громче: экран Тето ТИКАЕТ СЧЁТЧИК по раундам и грейдит большой выигрыш.

`animation` (`dict | null`, ТОЛЬКО у teto_slots) — покадровый сценарий спина
для анимированного игрового экрана миниаппа: доска 6x6 на каждом кадре,
подсветка выигравших мегаблоков ДО их удаления, каскадная гравитация,
посадка дрели Дрель-Ханта, HUD лестницы множителя. Полная схема (5 op'ов,
сегментация по раундам, пределы размера, денежный контракт) — в докстринге
`teto_slot_engine.serialize_animation`. Роут сам ничего не считает: заводит
пустой dict-sink, отдаёт его `casino_service.play_teto_slots` как
out-параметр и публикует под ключом `animation`.

--- КОНТРАКТ ФРОНТА /games/teto_slots (полный, читать перед экраном) -------

Ответ 200: `{game, bet, payout, outcome, user_balance_after, bank_capped,
animation}`. Ошибки: 400 (`InvalidBet`/`InsufficientFunds`), 401 (initData),
403 (не участник чата), 409 (`DuplicateRound` не разошёлся), 429
(`RateLimited` — авто-спин слишком частый).

  1. ДЕНЬГИ. Единственное авторитетное число — `payout`: ровно на столько
     изменился баланс игрока (`user_balance_after` — уже новый баланс).
     `outcome.total_payout` — ЧЕСТНЫЙ выигрыш ДО капа банком; он МОЖЕТ быть
     больше `payout`, и тогда `bank_capped == true`.
       - Счётчик выигрыша на экране обязан заканчиваться на `payout`.
       - Промежуточное значение после раунда k считать как
         `min(сумма animation.ops[...].final_round_payout по раундам 0..k,
         animation.payout_paid)` — `min` применять ВСЕГДА, тогда один и тот
         же код верен и в обычном, и в капнутом случае (`payout_paid` ==
         `payout`, дублируется в конверт, чтобы компонент счётчика не ходил
         за вторым числом в соседнюю ветку ответа).
       - Грейдинг ("большой выигрыш", хаптик, размер цифр) — ТОЛЬКО от
         `payout`. `payout_engine_total` (== `outcome.total_payout`) годится
         исключительно для честной подписи "выплачено X из Y — банк чата
         пуст" при `bank_capped`.
     Сумма `final_round_payout` по раундам НЕ равна `payout` при
     `bank_capped` — это не баг данных, а сам факт капа: движок распределяет
     по раундам неурезанный выигрыш, урезает его банк.
  2. `animation: null` — НОРМАЛЬНЫЙ ответ, не ошибка. Так выглядит любой
     раунд, который не считался в ЭТОМ запросе: идемпотентный replay уже
     рассчитанного `idem_key` (`_settle` возвращает сохранённый исход, не
     вызывая `compute()`) и ретрай по `DuplicateRound`, попавший на строку
     конкурентного запроса. Синтезировать анимацию там нельзя: повторный
     прокрут съел бы свежий RNG и показал бы игроку спин, которого не было,
     с выигрышем, не совпадающим с изменением баланса. Клиент в этом случае
     рисует финальную доску из `outcome.final_blocks`, лестницу из
     `outcome.ladder_final_state`, деньги из `payout`/`user_balance_after` —
     и это же правильное продуктовое поведение: replay это сетевой ретрай, а
     не новый раунд, игрок этот спин уже посмотрел. Отсутствие этого абзаца
     в документации кончилось бы фронтом, который считает отсутствие
     анимации ошибкой и показывает пустую доску после ретрая запроса.
     `bank_capped` при этом есть ВСЕГДА, включая replay.
  3. `animation.truncated == true` — трейс обрезан ЦЕЛЫМИ раундами по
     пределам размера/времени (`TRACE_MAX_OPS`/`TRACE_MAX_ROUNDS`):
     проигрываем раунды `0..complete_through_round`, затем прыгаем в финал по
     `outcome` и честно показываем, что `rounds_total - rounds_recorded`
     раундов пропущено. ⚠ Базовый раунд — это `round: 0`, а `0` в JS ложен:
     проверять `complete_through_round !== null`, никогда не по truthiness.
     Частичных раундов не бывает: усечение режет только хвост и только
     целыми раундами. Итоговый счётчик всё равно `payout` — усечение
     сокращает ПОКАЗ, а не деньги.
  4. Проигрывание: идти по `animation.ops` по порядку. `fill` открывает
     раунд, `round_end` закрывает; раунд идентифицируется парой
     (`phase`, `round`), где `phase == "base"` <=> `round == 0`, фриспины —
     1..N. Имена op'ов не содержат цифр и парсить их не нужно вовсе.
     `op.blocks` — полная доска этого кадра (та же кодировка, что
     `outcome.final_blocks`), поэтому seek на любой op рисуется немедленно,
     без переигрывания с нуля. `block_id` уникален В ПРЕДЕЛАХ СПИНА, включая
     границу раунда — по нему и твинится движение.
  5. Лестница множителя: статические пороги — `animation.ladder_thresholds`
     (`[{score, multiplier}, ...]`) и `animation.ladder_max_score`, прогресс
     — `round_end.ladder` (`score_after`/`multiplier_after`/
     `crossed_thresholds` + цель `next_threshold`/`next_multiplier`/
     `score_to_next`; `null` во всех трёх == все пороги пройдены). Копию
     порогов на клиенте не заводить. ⚠ Вариант Y: `multiplier_after`
     действует со СЛЕДУЮЩЕГО раунда, к текущему применён
     `round_end.multiplier_applied`.
  6. Деньги НИКОГДА не берутся из `outcome`-раундов напрямую как «итог»:
     `payout`/`bank_capped` — запись факта, `animation` — сценарий показа,
     который может отсутствовать или быть усечён.

Прецедент, из которого выросло требование: Azumanga
(`slot_engine.SlotResult.freespin_rounds`) — фронт раньше получал только
ИТОГОВОЕ ЧИСЛО фриспинов и просто бампал счётчик, что читалось игроками как
«авторасчёт, а не полный прокрут»; починка состояла в отдаче данных на
КАЖДЫЙ бонусный раунд. Здесь тот же урок применён на уровень глубже — к
каскадам ВНУТРИ раунда, которые до этого вообще не покидали движок.

`casino_service.RateLimited -> 429` — у slots И teto_slots (авто-спин в
miniapp: клиентский цикл повторных ставок без ручного тапа на каждый раунд,
см. `casino_service._check_slots_throttle`) — троттлинг-дикт `_last_slots_
spin_at` общий для обоих слотов казино (один и тот же анти-абьюз-концерн
по `user_id`, не форкается по игре, см. докстринг `_check_slots_throttle`);
coinflip/dice/roulette/blackjack не имеют авто-повтора, поэтому этой ветки
исключений у них нет.

POST /games/blackjack (start) + POST /games/blackjack/{game_id}/action
(04.2-10) — стейтфул-раздача (04.1-03): `game_id` из start-ответа
переиспользуется в /action; `user_id` — ТОЛЬКО из `AuthContext`, `game_id` —
ТОЛЬКО из пути (T-04.2-02, IDOR). `casino_service.blackjack_action`
фильтрует SELECT по `(id, user_id)` — попытка подействовать на чужую
раздачу структурно неотличима от несуществующей: `CasinoError` -> 404
(НЕ 403 — намеренно, не палим существование чужой раздачи). Действие на уже
`settled` раздаче — НЕ ошибка (T-04.1-09, уже протестировано/задокументировано
в `04.1-03-SUMMARY.md`): статус-переход "active"->"settled" сам служит
гардом идемпотентности, `blackjack_action` возвращает сохранённый исход
200-м ответом (повторный no-op) — роут это поведение не переопределяет.

`bank_capped` У БЛЭКДЖЕКА (оба роута выше) — тот же D-06-инцидент, что и у
coinflip/dice/roulette/teto_slots (см. выше), но вычисляется БЕЗУСЛОВНО, как
у teto_slots, а не только "если выиграл", как у coinflip/dice/roulette: у
блэкджека нет единого признака "won" — пять исходов (`natural`/`win`/`push`/
`lose`/`bust`), и даже `push` (честный возврат ставки, mult=1.0) идёт через
тот же `_finalize_blackjack` -> `economy_service.pay_from_bank` и может быть
урезан на пустом банке ровно как выигрыш — без безусловного вычисления кап
push'а остался бы недокументированной дырой. "Честная" выплата здесь не лежит
готовым числом в ответе (в отличие от Тето `outcome.total_payout`) — роут
восстанавливает её сам по `outcome.result` (см. `_BLACKJACK_FAIR_MULT`) и
ставке, той же дисциплиной, что и coinflip/dice/roulette выше (пересчёт по
уже доступным роуту данным, без правок settle-ядра). На ещё активной раздаче
(натурала не было, ход не завершён) `bank_capped` — честный `False`, а не
пропущенный ключ, той же логикой, что "total_payout == 0" у Тето.
"""

from __future__ import annotations

import html
import logging

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Request
from pydantic import BaseModel
from pydantic import Field
from sqlalchemy import select

from api import telegram_client
from api.deps import AuthContext
from api.deps import require_membership
from bot.config import settings
from bot.services import balance_events
from bot.services import casino_service
from bot.services import economy_service
from bot.services import jackpot_service
from common.db.session import SessionLocal
from common.models.user import User

router = APIRouter()

logger = logging.getLogger(__name__)

_jackpot_gif_bytes: bytes | None = None


class CoinflipBet(BaseModel):
    bet: int = Field(ge=1)
    choice: str
    idem_key: str


class DiceBet(BaseModel):
    bet: int = Field(ge=1)
    target: int
    direction: str
    idem_key: str


class RouletteBet(BaseModel):
    bet: int = Field(ge=1)
    bet_type: str
    bet_value: int | str
    idem_key: str


class SlotsBet(BaseModel):
    bet: int = Field(ge=1)
    idem_key: str


class BlackjackBet(BaseModel):
    bet: int = Field(ge=1)
    idem_key: str


class BlackjackActionBody(BaseModel):
    action: str  # 'hit' | 'stand' | 'double' — валидируется casino_service.blackjack_action


# bet_type -> "честный" (без D-06 капа) множитель выплаты, для bank_capped
# (см. модульный докстринг выше). Держится рядом с роутом, не в
# casino_service.py — та же причина, что у coinflip's fair_payout ниже.
_ROULETTE_FAIR_MULT: dict[str, int] = {
    "number": casino_service.ROULETTE_NUMBER_MULT,
    "color": casino_service.ROULETTE_EVEN_MULT,
    "parity": casino_service.ROULETTE_EVEN_MULT,
    "half": casino_service.ROULETTE_EVEN_MULT,
    "dozen": casino_service.ROULETTE_DOZEN_MULT,
}

# outcome.result -> "честный" (без D-06 капа) множитель выплаты блэкджека,
# для bank_capped у post_blackjack_start/post_blackjack_action ниже. Держится
# рядом с роутом, та же причина, что у _ROULETTE_FAIR_MULT выше — не общее
# settle-ядро ради одного UI-флага. Зеркалит blackjack_engine.settle_outcome()
# и BLACKJACK_NATURAL_MULT: "lose"/"bust" честно платят 0. Отсутствующий ключ
# (раздача ещё "active", settle не случился) через `.get(..., 0.0)` тоже даёт
# мультипликатор 0 — не отдельная ветка, а тот же путь, что и настоящий
# проигрыш (см. комментарий у post_blackjack_start).
_BLACKJACK_FAIR_MULT: dict[str, float] = {
    "natural": casino_service.BLACKJACK_NATURAL_MULT,
    "win": casino_service.BLACKJACK_WIN_MULT,
    "push": casino_service.BLACKJACK_PUSH_MULT,
    "lose": 0.0,
    "bust": 0.0,
}


@router.post("/api/v1/games/coinflip")
async def post_coinflip(
    body: CoinflipBet, request: Request, auth: AuthContext = Depends(require_membership)
) -> dict:
    async with SessionLocal() as session:
        try:
            result = await _play(session, auth, body)
        except casino_service.DuplicateRound:
            try:
                result = await _play(session, auth, body)
            except casino_service.DuplicateRound as exc:
                raise HTTPException(status_code=409, detail="round in progress, retry") from exc
        except casino_service.GameNotActive as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except (casino_service.InvalidBet, economy_service.InsufficientFunds) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        balance = await economy_service.get_balance(session, auth.chat_id, auth.user_id)
        await balance_events.publish_balance(
            request.app.state.redis, auth.chat_id, auth.user_id, balance
        )
        result["user_balance_after"] = balance

        outcome = result.get("outcome") or {}
        if outcome.get("won"):
            fair_payout = int(result["bet"] * casino_service.COINFLIP_MULT)
            result["bank_capped"] = result["payout"] < fair_payout

        return result


async def _play(session, auth: AuthContext, body: CoinflipBet) -> dict:
    return await casino_service.play_coinflip(
        session, auth.chat_id, auth.user_id, body.bet, body.choice, body.idem_key
    )


@router.post("/api/v1/games/dice")
async def post_dice(
    body: DiceBet, request: Request, auth: AuthContext = Depends(require_membership)
) -> dict:
    async with SessionLocal() as session:
        try:
            result = await _play_dice(session, auth, body)
        except casino_service.DuplicateRound:
            try:
                result = await _play_dice(session, auth, body)
            except casino_service.DuplicateRound as exc:
                raise HTTPException(status_code=409, detail="round in progress, retry") from exc
        except casino_service.GameNotActive as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except (casino_service.InvalidBet, economy_service.InsufficientFunds) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        balance = await economy_service.get_balance(session, auth.chat_id, auth.user_id)
        await balance_events.publish_balance(
            request.app.state.redis, auth.chat_id, auth.user_id, balance
        )
        result["user_balance_after"] = balance

        outcome = result.get("outcome") or {}
        if outcome.get("won"):
            fair_payout = casino_service.dice_fair_payout(result["bet"], body.target, body.direction)
            result["bank_capped"] = result["payout"] < fair_payout

        return result


async def _play_dice(session, auth: AuthContext, body: DiceBet) -> dict:
    return await casino_service.play_dice(
        session, auth.chat_id, auth.user_id, body.bet, body.target, body.direction, body.idem_key
    )


@router.post("/api/v1/games/roulette")
async def post_roulette(
    body: RouletteBet, request: Request, auth: AuthContext = Depends(require_membership)
) -> dict:
    async with SessionLocal() as session:
        try:
            result = await _play_roulette(session, auth, body)
        except casino_service.DuplicateRound:
            try:
                result = await _play_roulette(session, auth, body)
            except casino_service.DuplicateRound as exc:
                raise HTTPException(status_code=409, detail="round in progress, retry") from exc
        except casino_service.GameNotActive as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except (casino_service.InvalidBet, economy_service.InsufficientFunds) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        balance = await economy_service.get_balance(session, auth.chat_id, auth.user_id)
        await balance_events.publish_balance(
            request.app.state.redis, auth.chat_id, auth.user_id, balance
        )
        result["user_balance_after"] = balance

        outcome = result.get("outcome") or {}
        if outcome.get("won"):
            fair_mult = _ROULETTE_FAIR_MULT[body.bet_type]
            fair_payout = int(result["bet"] * fair_mult)
            result["bank_capped"] = result["payout"] < fair_payout

        return result


async def _play_roulette(session, auth: AuthContext, body: RouletteBet) -> dict:
    return await casino_service.play_roulette(
        session, auth.chat_id, auth.user_id, body.bet, body.bet_type, body.bet_value, body.idem_key
    )


@router.get("/api/v1/games/slots/jackpot")
async def get_slots_jackpot(auth: AuthContext = Depends(require_membership)) -> dict:
    """Текущий размер пула джекпота (CASINO-06) для тикера на экране слота —
    читается БЕЗ блокировки строки (`jackpot_service.get_pool`), значение
    только информационное до следующего спина; авторитетное обновление пула
    приходит в ответе `POST /games/slots` (`result["jackpot"]["pool"]`)."""
    async with SessionLocal() as session:
        pool = await jackpot_service.get_pool(session, auth.chat_id)
    return {"pool": pool}


def _load_jackpot_gif() -> bytes:
    """Читает `jackpot_service.JACKPOT_GIF_PATH` с диска один раз и кэширует
    в памяти процесса — файл маленький (~5 МБ) и неизменный, перечитывать его
    на каждый выигрыш смысла нет."""
    global _jackpot_gif_bytes
    if _jackpot_gif_bytes is None:
        _jackpot_gif_bytes = jackpot_service.JACKPOT_GIF_PATH.read_bytes()
    return _jackpot_gif_bytes


async def _announce_jackpot_win(
    request: Request, session, chat_id: int, user_id: int, amount: int, pool_after: int
) -> None:
    """Публикует срыв джекпота слота в чат — гифка + подпись с именем
    победителя и суммой (CASINO-06). Best-effort, та же дисциплина, что
    `shop.py::_deliver_to_chat`: недоставленное сообщение (сетевой сбой
    Telegram, отсутствующий файл на диске) НЕ должно откатывать уже
    совершённую и закоммиченную выплату — вызывается ПОСЛЕ `_play_slots`
    успешно вернул(а) `won=True`, деньги уже у игрока независимо от исхода
    этого вызова."""
    name = (
        await session.execute(select(User.first_name).where(User.id == user_id))
    ).scalar_one_or_none() or str(user_id)

    caption = jackpot_service.build_announcement_caption(html.escape(name), amount, pool_after)
    try:
        gif_bytes = _load_jackpot_gif()
    except OSError:
        logger.exception("_announce_jackpot_win: не удалось прочитать jackpot.gif")
        return

    result = await telegram_client.send_animation(
        request.app.state.http_client,
        settings.bot_token,
        chat_id,
        gif_bytes,
        "jackpot.gif",
        caption=caption,
        parse_mode="HTML",
    )
    if not result.get("ok"):
        logger.warning(
            "_announce_jackpot_win: sendAnimation не доставлен chat_id=%s: %s", chat_id, result
        )


@router.post("/api/v1/games/slots")
async def post_slots(
    body: SlotsBet, request: Request, auth: AuthContext = Depends(require_membership)
) -> dict:
    async with SessionLocal() as session:
        try:
            result = await _play_slots(session, auth, body)
        except casino_service.DuplicateRound:
            try:
                result = await _play_slots(session, auth, body)
            except casino_service.DuplicateRound as exc:
                raise HTTPException(status_code=409, detail="round in progress, retry") from exc
        except casino_service.GameNotActive as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except casino_service.RateLimited as exc:
            raise HTTPException(status_code=429, detail=str(exc)) from exc
        except (casino_service.InvalidBet, economy_service.InsufficientFunds) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        balance = await economy_service.get_balance(session, auth.chat_id, auth.user_id)
        await balance_events.publish_balance(
            request.app.state.redis, auth.chat_id, auth.user_id, balance
        )
        result["user_balance_after"] = balance

        jackpot = result.get("jackpot")
        if jackpot and jackpot.get("won") and jackpot.get("amount", 0) > 0:
            await _announce_jackpot_win(
                request, session, auth.chat_id, auth.user_id, jackpot["amount"], jackpot["pool"]
            )

        return result


async def _play_slots(session, auth: AuthContext, body: SlotsBet) -> dict:
    return await casino_service.play_slots(session, auth.chat_id, auth.user_id, body.bet, body.idem_key)


@router.post("/api/v1/games/teto_slots")
async def post_teto_slots(
    body: SlotsBet, request: Request, auth: AuthContext = Depends(require_membership)
) -> dict:
    """Слот "Тето Брейнрот: Дрель-Хант" — тот же тонкий паттерн, что и
    POST /games/slots выше (см. модульный докстринг): `casino_service.
    play_teto_slots` уже несёт всю RNG/каскад/фриспин/лестница-логику, роут
    только прокидывает `bet`/`idem_key`, маппит исключения на HTTP и
    публикует баланс. Без jackpot-ветки (`play_teto_slots` не возвращает
    ключ "jackpot") — намеренно, см. модульный докстринг.

    `bank_capped` — БЕЗУСЛОВНО (не только на выигрыше, как у coinflip/dice/
    roulette): "честная" выплата Тето лежит прямо в ответе как
    `outcome.total_payout`, сравнивать её с уже капнутым `payout` роут может
    без единого лишнего запроса и без правок settle-ядра. Полное обоснование
    (и почему аргумент Azumanga "роут её не видит" здесь не работает) — в
    модульном докстринге. `outcome` у Тето непустой всегда, но `.get` вместо
    индексации — чтобы гипотетический сохранённый исход без ключа давал
    честный `false`, а не 500 поверх уже проведённых денег.

    ЕДИНСТВЕННОЕ, что роут делает сверх этого — прокидывает dict-sink под
    покадровый сценарий анимации и публикует его как top-level ключ
    `animation` (`dict | null`). Почему именно так:
      - sink, а не новый ключ в возврате `play_teto_slots`: её возвращаемое
        значение обязано остаться идентичным между свежим спином и replay,
        см. её докстринг;
      - `animation.clear()` перед КАЖДОЙ попыткой, включая ретрай по
        `DuplicateRound`: иначе на ретрае, попавшем на строку конкурентного
        запроса, наружу уехал бы трейс ПЕРВОЙ (выброшенной) попытки при
        `outcome` от чужого спина — анимация одного спина с деньгами другого;
      - `animation or None`: пустой dict (replay — `compute()` не вызывался
        вовсе) отдаётся как честный `null`, а не как `{}`, чтобы клиенту не
        приходилось различать "анимации нет" и "анимация пустая". Полный
        контракт для клиента — в модульном докстринге выше."""
    animation: dict = {}
    async with SessionLocal() as session:
        try:
            animation.clear()
            result = await _play_teto_slots(session, auth, body, animation)
        except casino_service.DuplicateRound:
            try:
                animation.clear()
                result = await _play_teto_slots(session, auth, body, animation)
            except casino_service.DuplicateRound as exc:
                raise HTTPException(status_code=409, detail="round in progress, retry") from exc
        except casino_service.GameNotActive as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except casino_service.RateLimited as exc:
            raise HTTPException(status_code=429, detail=str(exc)) from exc
        except (casino_service.InvalidBet, economy_service.InsufficientFunds) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        balance = await economy_service.get_balance(session, auth.chat_id, auth.user_id)
        await balance_events.publish_balance(
            request.app.state.redis, auth.chat_id, auth.user_id, balance
        )
        result["user_balance_after"] = balance
        result["bank_capped"] = result["payout"] < (result.get("outcome") or {}).get(
            "total_payout", 0
        )
        result["animation"] = animation or None

        return result


async def _play_teto_slots(
    session, auth: AuthContext, body: SlotsBet, animation_sink: dict | None = None
) -> dict:
    return await casino_service.play_teto_slots(
        session, auth.chat_id, auth.user_id, body.bet, body.idem_key,
        animation_sink=animation_sink,
    )


@router.post("/api/v1/games/blackjack")
async def post_blackjack_start(
    body: BlackjackBet, request: Request, auth: AuthContext = Depends(require_membership)
) -> dict:
    async with SessionLocal() as session:
        try:
            result = await _start_blackjack(session, auth, body)
        except casino_service.DuplicateRound:
            try:
                result = await _start_blackjack(session, auth, body)
            except casino_service.DuplicateRound as exc:
                raise HTTPException(status_code=409, detail="round in progress, retry") from exc
        except casino_service.GameNotActive as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except (casino_service.InvalidBet, economy_service.InsufficientFunds) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        balance = await economy_service.get_balance(session, auth.chat_id, auth.user_id)
        await balance_events.publish_balance(
            request.app.state.redis, auth.chat_id, auth.user_id, balance
        )
        result["user_balance_after"] = balance

        # bank_capped (D-06, см. модульный докстринг) — считается БЕЗУСЛОВНО,
        # не только на выигрыше: у блэкджека нет отдельного признака "won",
        # как у coinflip/dice/roulette, — пять исходов ("natural"/"win"/
        # "push"/"lose"/"bust"), и даже "push" (mult=1.0, честный возврат
        # ставки) идёт через тот же `_finalize_blackjack` -> `pay_from_bank`
        # и может быть урезан на пустом банке (тот же класс инцидента, что и
        # у выигрыша) — тот же паттерн, что у teto_slots ниже, а не у
        # coinflip/dice/roulette выше (см. их докстринг-обоснование). Раздача
        # ещё "active" (натурала не было, `_blackjack_view` не кладёт ключи
        # "outcome"/"payout" вовсе) не требует отдельной ветки: `.get(None,
        # 0.0)` даёт fair_mult=0, `result.get("payout", 0)` — 0, `0 < 0` ->
        # честный `False`.
        outcome = result.get("outcome") or {}
        fair_mult = _BLACKJACK_FAIR_MULT.get(outcome.get("result"), 0.0)
        fair_payout = int(result["bet"] * fair_mult)
        result["bank_capped"] = result.get("payout", 0) < fair_payout

        return result


async def _start_blackjack(session, auth: AuthContext, body: BlackjackBet) -> dict:
    return await casino_service.start_blackjack(
        session, auth.chat_id, auth.user_id, body.bet, body.idem_key
    )


@router.post("/api/v1/games/blackjack/{game_id}/action")
async def post_blackjack_action(
    game_id: int,
    body: BlackjackActionBody,
    request: Request,
    auth: AuthContext = Depends(require_membership),
) -> dict:
    async with SessionLocal() as session:
        try:
            result = await casino_service.blackjack_action(
                session, auth.chat_id, game_id, auth.user_id, body.action
            )
        except casino_service.GameNotActive as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except (casino_service.InvalidBet, economy_service.InsufficientFunds) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except casino_service.CasinoError as exc:
            # game_id не найден ДЛЯ ЭТОГО user_id (T-04.2-02 IDOR — SELECT в
            # blackjack_action фильтрует по user_id, чужая раздача структурно
            # неотличима от несуществующей) — 404, не 403 (не палим факт
            # существования чужой раздачи).
            raise HTTPException(status_code=404, detail=str(exc)) from exc

        balance = await economy_service.get_balance(session, auth.chat_id, auth.user_id)
        await balance_events.publish_balance(
            request.app.state.redis, auth.chat_id, auth.user_id, balance
        )
        result["user_balance_after"] = balance

        # bank_capped — тот же паттерн, что и post_blackjack_start выше (см.
        # его комментарий и модульный докстринг): считается БЕЗУСЛОВНО, а не
        # только на "win"/"natural" (push тоже платит через pay_from_bank и
        # тоже может быть урезан). Единственное отличие от start: `double`
        # удваивает СТАВКУ в деньгах, но `game_row.bet` (=`result["bet"]`)
        # остаётся исходным (`blackjack_action` его не переписывает, см.
        # casino_service) — честный payout удвоенной раздачи считаем по
        # ставке этого запроса ×2, используя `body.action` ТЕКУЩЕГО вызова
        # (тот же контракт, что у body.bet_type/target для roulette/dice
        # выше: предполагается, что клиент шлёт тот же action, которым
        # раздача была реально settled — верно для обычного UI-флоу "нажал ->
        # получил ответ"). Не переживает гипотетический no-op replay
        # (T-04.1-09) с ДРУГИМ action на уже settled раздаче, чем той, что её
        # реально settled'ила — тот же класс документированного, а не тихого
        # пробела, что и bank_capped-разрыв слотов в модульном докстринге.
        outcome = result.get("outcome") or {}
        fair_mult = _BLACKJACK_FAIR_MULT.get(outcome.get("result"), 0.0)
        effective_bet = result["bet"] * (2 if body.action == "double" else 1)
        fair_payout = int(effective_bet * fair_mult)
        result["bank_capped"] = result.get("payout", 0) < fair_payout

        return result
