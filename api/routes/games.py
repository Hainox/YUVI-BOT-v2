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
ради одного UI-флага не нужно. dice/roulette (04.2-03) повторяют тот же
паттерн, что и coinflip (04.2-02), пересчитывая свою "честную" формулу
выплаты по данным, уже доступным роуту (target/direction для dice,
bet_type для roulette), а не дублируя приватную `compute()`-логику
`casino_service`.

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
`bank_capped` НЕ вычисляется по той же причине, что и у слотов выше — даже
более явно ввиду каскадно-фриспин-лестничной сложности Тето: "честная"
выплата известна только внутри `play_teto_slots.compute()` до капа банком.

`animation` (`dict | null`, ТОЛЬКО у teto_slots) — покадровый сценарий спина
для анимированного игрового экрана миниаппа: доска 6x6 на каждом кадре,
подсветка выигравших мегаблоков ДО их удаления, каскадная гравитация,
посадка дрели Дрель-Ханта, HUD лестницы множителя. Полная схема (5 op'ов,
сегментация по раундам, пределы размера) — в докстринге
`teto_slot_engine.serialize_animation`. Роут сам ничего не считает: заводит
пустой dict-sink, отдаёт его `casino_service.play_teto_slots` как
out-параметр и публикует под ключом `animation`.

Три вещи, которые ОБЯЗАН знать клиент этого эндпоинта:
  1. `animation: null` — НОРМАЛЬНЫЙ ответ, не ошибка. Так выглядит любой
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
  2. `animation.truncated == true` — трейс обрезан ЦЕЛЫМИ раундами по
     пределам размера/времени (`TRACE_MAX_OPS`/`TRACE_MAX_ROUNDS`):
     проигрываем раунды `0..complete_through_round`, затем прыгаем в финал по
     `outcome` и честно показываем, что `rounds_total - rounds_recorded`
     раундов пропущено. ⚠ Базовый раунд — это `round: 0`, а `0` в JS ложен:
     проверять `complete_through_round !== null`, никогда не по truthiness.
  3. Деньги НИКОГДА не берутся из анимации: `payout`/`outcome` — запись
     факта, `animation` — сценарий показа, который может отсутствовать или
     быть усечён.

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
            win_prob = (
                (body.target - 1) / 100 if body.direction == "under" else (100 - body.target) / 100
            )
            fair_payout = int(result["bet"] * (1 - casino_service.DICE_HOUSE_EDGE) / win_prob)
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
    ключ "jackpot") и без `bank_capped` — намеренно, см. модульный
    докстринг.

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

        return result
