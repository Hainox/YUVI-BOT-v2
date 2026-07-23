"""P2P-биржа ювиков (EXCHANGE-01) — листинг/claim/cancel/confirm, плюс
админский force-cancel/force-release для споров по зависшим листингам.

Продавец выставляет ювики на продажу с описанием желаемой оплаты свободным
текстом (`want_description`, НЕ структурированная цена) — сама оплата
происходит ВНЕ бота, между двумя живыми людьми. Бот эскроирует ТОЛЬКО
ювик-сторону сделки: он не может ни проверить, ни гарантировать, что
покупатель реально заплатил продавцу что-то за пределами платформы.

Зачем вообще escrow -> claim -> seller confirms, а не наивный "перевёл по
клику": наивный дизайн даёт продавцу выставить ювики, получить оплату вне
бота и просто не отдать их (rug pull), либо даёт покупателю никогда не
заплатить, при этом получив ювики. Escrow здесь решает ТОЛЬКО первую половину
(продавец физически не может передумать и заскамить уже после claim —
эскроу вне его баланса) — вторую половину (покупатель не платит) бот
принципиально решить не может, поэтому это по-прежнему flow, построенный
на живом доверии между двумя людьми чата: seller видит, кто именно claim'нул
листинг, и просто не вызывает confirm, пока реально не получит оплату.
cancel_listing (для ещё-открытого листинга) и admin_force_cancel/
admin_force_release (для зависших claimed-листингов, когда стороны не
могут договориться) — последние рубежи, не гарантия результата сделки.

Деньги двигает ТОЛЬКО через `bot.services.economy_service` (debit —
эскроу при создании, credit — рефанд/релиз) — этот модуль НИКОГДА не пишет
user_balance/chat_bank/economy_tx напрямую (economy_service.py — единственный
модуль с таким правом, см. его докстринг).

WR (форма duel_service WR-04 "ставка не заходит в chat_bank, пока pending"):
эскроированные ювики НИКОГДА не заходят в chat_bank — ни на create_listing,
ни на claim_listing (claim — не платёж, просто статус-переход "я в деле").
Полный рефанд (cancel_listing/admin_force_cancel) и релиз покупателю
(confirm_fulfillment/admin_force_release) идут прямым `economy_service.credit`,
а не через `pay_from_bank` — это деньги продавца, ни разу не поставленные ни
на что игровое, поэтому D-06 bank-cap здесь неприменим (та же причина, что у
`duel_service._refund_pending_duel`): рефанд/релиз гарантированно полный,
независимо от текущего остатка банка чата.

Идемпотентность:
- create_listing — одноразовая операция, ref_id передаётся вызывающим;
  повтор с тем же ref_id ловится `economy_service.debit` (возвращает False)
  и поднимает ListingAlreadyResolved (форма duel_service.create_duel — не
  искать/возвращать уже созданный листинг, а явно сообщить о повторе).
- cancel_listing/confirm_fulfillment/admin_force_cancel/admin_force_release —
  статус-переход листинга (open -> claimed -> fulfilled, либо -> cancelled)
  САМ служит гардом идемпотентности (форма markets_service.resolve_market/
  cancel_market): повторный вызов на уже неактуальном статусе — no-op,
  деньги не двигаются повторно.
- claim_listing — тоже статус-переход-как-гард, но денег не двигает вовсе
  (см. выше) — повторный/гоночный claim на уже claimed-листинге — no-op.

Контракт порядка блокировок: строка ExchangeListing блокируется FOR UPDATE
ПЕРВОЙ (замораживает status), затем двигаются деньги — та же форма, что
duel_service.accept_duel/markets_service.place_bet (row-first). Каждый вызов
трогает не более одной строки user_balance за раз (в отличие от
transfer_with_fee) — сортировка user_id между двумя сторонами здесь не нужна.

item_type/gacha_char_id — колонки уже в схеме на будущее (гача-карты), но
сам флоу карточных листингов НЕ реализован в этой фазе (гача выключена
флагом GACHA_DISABLED в miniapp/src/routes/gacha/+page.svelte) —
create_listing поднимает ExchangeError на любом item_type, кроме "yuvik".
"""

from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from bot.config import settings
from bot.services import economy_service
from common.models.exchange_listing import ExchangeListing
from common.models.user import User

logger = logging.getLogger(__name__)

STATUS_OPEN = "open"
STATUS_CLAIMED = "claimed"
STATUS_FULFILLED = "fulfilled"
STATUS_CANCELLED = "cancelled"

# D-05 (форма markets_service.QUESTION_MAX_LEN): свободный текст того, что
# продавец хочет взамен — не структурированная цена, валидируется только по
# длине и непустоте.
WANT_DESCRIPTION_MAX_LEN = 300

ITEM_TYPES = frozenset({"yuvik", "gacha_card"})


# --- Исключения ---------------------------------------------------------


class ExchangeError(Exception):
    """Базовое исключение модуля биржи."""


class ListingNotFound(ExchangeError):
    """Листинг с указанным id не найден в этом чате."""


class ListingAlreadyResolved(ExchangeError):
    """Повторный запрос с тем же ref_id (create_listing) уже обработан —
    идемпотентный no-op, листинг повторно не создаётся."""


# --- Валидация -----------------------------------------------------------


def _validate_amount(amount: int) -> None:
    """Сумма листинга — минимум тот же порог, что у казино/дуэлей (D-04),
    переиспользуем settings.casino_min_bet, отдельного порога не заводим."""
    if amount < settings.casino_min_bet:
        raise ExchangeError(f"Минимальная сумма листинга — {settings.casino_min_bet} ювиков")


def _validate_want_description(raw: str) -> str:
    text = raw.strip()
    if not text:
        raise ExchangeError("Опиши, что хочешь получить взамен")
    if len(text) > WANT_DESCRIPTION_MAX_LEN:
        raise ExchangeError(f"Описание не длиннее {WANT_DESCRIPTION_MAX_LEN} символов")
    return text


async def _get_listing_for_update(session: AsyncSession, chat_id: int, listing_id: int) -> ExchangeListing:
    listing = (
        await session.execute(
            select(ExchangeListing)
            .where(ExchangeListing.chat_id == chat_id, ExchangeListing.id == listing_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if listing is None:
        raise ListingNotFound(f"Листинг #{listing_id} не найден")
    return listing


# --- create_listing (эскроу продавца) ---------------------------------------


async def create_listing(
    session: AsyncSession,
    chat_id: int,
    seller_user_id: int,
    yuvik_amount: int,
    want_description: str,
    ref_id: str,
    item_type: str = "yuvik",
    gacha_char_id: str | None = None,
) -> ExchangeListing:
    """Создаёт листинг: валидирует сумму/описание, эскроирует ювики продавца
    (`economy_service.debit`, kind="exchange_escrow"), вставляет
    ExchangeListing(status="open"). Поднимает ExchangeError при нарушении
    валидации (ДО любого движения денег), economy_service.InsufficientFunds
    при нехватке средств, ListingAlreadyResolved при повторе ref_id
    (идемпотентный no-op — листинг повторно не создаётся)."""
    if item_type not in ITEM_TYPES:
        raise ExchangeError("Неизвестный тип листинга")
    if item_type != "yuvik":
        raise ExchangeError("Листинги гача-карт пока не поддерживаются")

    _validate_amount(yuvik_amount)
    description = _validate_want_description(want_description)

    escrowed = await economy_service.debit(
        session, chat_id, seller_user_id, yuvik_amount, kind="exchange_escrow", ref_id=ref_id
    )
    if not escrowed:
        logger.info("create_listing: ref_id=%s уже обработан, пропускаем", ref_id)
        raise ListingAlreadyResolved(f"Запрос на создание листинга уже обработан (ref_id={ref_id})")

    listing = ExchangeListing(
        chat_id=chat_id,
        seller_user_id=seller_user_id,
        yuvik_amount=yuvik_amount,
        want_description=description,
        status=STATUS_OPEN,
        item_type=item_type,
        gacha_char_id=gacha_char_id,
    )
    session.add(listing)
    await session.commit()
    return listing


# --- claim_listing (мягкий сигнал координации, деньги НЕ двигаются) --------


async def claim_listing(session: AsyncSession, chat_id: int, listing_id: int, buyer_id: int) -> dict:
    """Помечает листинг как claimed текущим покупателем — сигнал "я
    договариваюсь с продавцом вне бота", НЕ платёж. `SELECT ... FOR UPDATE`
    первым сериализует конкурентные claim на один листинг: кто первый
    захватил лок, тот и переводит статус в "claimed", остальные видят уже
    не-open статус и получают идемпотентный no-op (гоночная защита от
    двойного claim). Поднимает ListingNotFound; ExchangeError при попытке
    заклеймить собственный листинг (self-trade guard, форма
    duel_service self-duel guard в create_duel)."""
    listing = await _get_listing_for_update(session, chat_id, listing_id)
    if listing.status != STATUS_OPEN:
        await session.commit()
        return {"status": listing.status, "listing_id": listing_id, "claimed": False}

    if listing.seller_user_id == buyer_id:
        await session.commit()
        raise ExchangeError("Нельзя заклеймить собственный листинг")

    listing.status = STATUS_CLAIMED
    listing.claimed_by_user_id = buyer_id
    listing.claimed_at = datetime.utcnow()
    await session.commit()
    return {
        "status": STATUS_CLAIMED,
        "listing_id": listing_id,
        "claimed": True,
        "claimed_by_user_id": buyer_id,
    }


# --- cancel_listing (продавец, только пока open, полный рефанд) ------------


async def cancel_listing(session: AsyncSession, chat_id: int, listing_id: int, actor_id: int) -> dict:
    """Отменяет ещё-открытый листинг (только продавец) — полный рефанд
    эскроу. Статус-переход — гард идемпотентности: повтор на уже
    неоткрытом листинге — no-op. Поднимает ListingNotFound; ExchangeError,
    если actor_id не продавец."""
    listing = await _get_listing_for_update(session, chat_id, listing_id)
    if listing.status != STATUS_OPEN:
        await session.commit()
        return {"status": listing.status, "listing_id": listing_id, "refunded": 0}

    if listing.seller_user_id != actor_id:
        await session.commit()
        raise ExchangeError("Только продавец может отменить свой листинг")

    refunded_ok = await economy_service.credit(
        session,
        chat_id,
        listing.seller_user_id,
        listing.yuvik_amount,
        kind="exchange_refund",
        ref_id=f"exchange:{listing_id}:refund",
    )
    listing.status = STATUS_CANCELLED
    listing.resolved_at = datetime.utcnow()
    await session.commit()
    return {
        "status": STATUS_CANCELLED,
        "listing_id": listing_id,
        "refunded": listing.yuvik_amount if refunded_ok else 0,
    }


# --- confirm_fulfillment (продавец подтверждает оплату вне бота) -----------


async def confirm_fulfillment(
    session: AsyncSession, chat_id: int, listing_id: int, actor_id: int, ref_id: str
) -> dict:
    """Продавец подтверждает, что реально получил оплату вне бота —
    освобождает эскроу заклеймившему покупателю. Только продавец, только
    пока status == "claimed". Статус-переход — гард идемпотентности: повтор
    на уже не-claimed листинге — no-op (деньги не двигаются повторно), плюс
    `ref_id` на самом `credit` — тот же двойной пояс, что у
    duel_service.accept_duel. Поднимает ListingNotFound; ExchangeError, если
    actor_id не продавец."""
    listing = await _get_listing_for_update(session, chat_id, listing_id)
    if listing.status != STATUS_CLAIMED:
        await session.commit()
        return {"status": listing.status, "listing_id": listing_id, "released": 0}

    if listing.seller_user_id != actor_id:
        await session.commit()
        raise ExchangeError("Только продавец может подтвердить сделку")

    claimed_by = listing.claimed_by_user_id
    released_ok = await economy_service.credit(
        session, chat_id, claimed_by, listing.yuvik_amount, kind="exchange_release", ref_id=ref_id
    )
    listing.status = STATUS_FULFILLED
    listing.resolved_at = datetime.utcnow()
    await session.commit()
    return {
        "status": STATUS_FULFILLED,
        "listing_id": listing_id,
        "released": listing.yuvik_amount if released_ok else 0,
        "claimed_by_user_id": claimed_by,
    }


# --- Admin dispute resolution ------------------------------------------------
# Актор здесь НЕ гейтится (форма markets_service/duel_service — админ-проверка
# живёт у вызывающего): bot/handlers/exchange.py гейтит admin_service.
# is_chat_admin (форма bot/handlers/duel.py::unmute_command), Mini App API
# намеренно не выставляет эти два эндпоинта (см. api/routes/exchange.py).


async def admin_force_cancel(session: AsyncSession, chat_id: int, listing_id: int) -> dict:
    """Принудительная отмена спорного листинга (open ИЛИ claimed) — полный
    рефанд продавцу независимо от того, был ли листинг кем-то claim'нут.
    Статус-переход — гард идемпотентности: уже fulfilled/cancelled — no-op."""
    listing = await _get_listing_for_update(session, chat_id, listing_id)
    if listing.status not in (STATUS_OPEN, STATUS_CLAIMED):
        await session.commit()
        return {"status": listing.status, "listing_id": listing_id, "refunded": 0}

    refunded_ok = await economy_service.credit(
        session,
        chat_id,
        listing.seller_user_id,
        listing.yuvik_amount,
        kind="exchange_admin_refund",
        ref_id=f"exchange:{listing_id}:admin_refund",
    )
    listing.status = STATUS_CANCELLED
    listing.resolved_at = datetime.utcnow()
    await session.commit()
    return {
        "status": STATUS_CANCELLED,
        "listing_id": listing_id,
        "refunded": listing.yuvik_amount if refunded_ok else 0,
    }


async def admin_force_release(session: AsyncSession, chat_id: int, listing_id: int) -> dict:
    """Принудительный релиз эскроу заклеймившему покупателю — только для
    claimed-листинга (у open-листинга нет claimed_by_user_id, релизить
    некому — поднимает ExchangeError). Статус-переход — гард
    идемпотентности: уже fulfilled/cancelled — no-op."""
    listing = await _get_listing_for_update(session, chat_id, listing_id)
    if listing.status in (STATUS_FULFILLED, STATUS_CANCELLED):
        await session.commit()
        return {"status": listing.status, "listing_id": listing_id, "released": 0}
    if listing.status != STATUS_CLAIMED:
        await session.commit()
        raise ExchangeError("Принудительный релиз доступен только для claimed-листинга")

    claimed_by = listing.claimed_by_user_id
    released_ok = await economy_service.credit(
        session,
        chat_id,
        claimed_by,
        listing.yuvik_amount,
        kind="exchange_admin_release",
        ref_id=f"exchange:{listing_id}:admin_release",
    )
    listing.status = STATUS_FULFILLED
    listing.resolved_at = datetime.utcnow()
    await session.commit()
    return {
        "status": STATUS_FULFILLED,
        "listing_id": listing_id,
        "released": listing.yuvik_amount if released_ok else 0,
        "claimed_by_user_id": claimed_by,
    }


# --- Read-хелперы (без записи) ----------------------------------------------


async def get_open_listings(session: AsyncSession, chat_id: int) -> list[dict]:
    """Открытые листинги чата — для /exchange (список в боте) и хаба Mini
    App. Джойнит `users` за именем продавца, чтобы клиент не делал отдельный
    запрос за именем (форма economy_service.get_leaderboard)."""
    rows = (
        await session.execute(
            select(
                ExchangeListing.id,
                ExchangeListing.seller_user_id,
                User.first_name.label("seller_name"),
                ExchangeListing.yuvik_amount,
                ExchangeListing.want_description,
                ExchangeListing.item_type,
                ExchangeListing.created_at,
            )
            .join(User, User.id == ExchangeListing.seller_user_id)
            .where(ExchangeListing.chat_id == chat_id, ExchangeListing.status == STATUS_OPEN)
            .order_by(ExchangeListing.created_at.desc())
        )
    ).all()
    return [
        {
            "id": row.id,
            "seller_user_id": row.seller_user_id,
            "seller_name": row.seller_name,
            "yuvik_amount": row.yuvik_amount,
            "want_description": row.want_description,
            "item_type": row.item_type,
            "created_at": row.created_at,
        }
        for row in rows
    ]


async def get_my_listings(session: AsyncSession, chat_id: int, user_id: int) -> list[dict]:
    """Листинги участника в этом чате — и как продавца (для cancel/confirm
    в Mini App/боте), и как покупателя (claim'нутые им листинги, чтобы
    видеть статус сделки). `role` в каждой строке различает эти две роли на
    клиенте; листинг, где участник одновременно продавец и покупатель,
    структурно невозможен (self-trade guard в claim_listing)."""
    Buyer = aliased(User)
    seller_rows = (
        await session.execute(
            select(
                ExchangeListing.id,
                ExchangeListing.status,
                ExchangeListing.yuvik_amount,
                ExchangeListing.want_description,
                ExchangeListing.item_type,
                ExchangeListing.seller_user_id,
                User.first_name.label("seller_name"),
                ExchangeListing.claimed_by_user_id,
                Buyer.first_name.label("claimed_by_name"),
                ExchangeListing.created_at,
                ExchangeListing.claimed_at,
                ExchangeListing.resolved_at,
            )
            .join(User, User.id == ExchangeListing.seller_user_id)
            .outerjoin(Buyer, Buyer.id == ExchangeListing.claimed_by_user_id)
            .where(ExchangeListing.chat_id == chat_id, ExchangeListing.seller_user_id == user_id)
            .order_by(ExchangeListing.created_at.desc())
        )
    ).all()
    buyer_rows = (
        await session.execute(
            select(
                ExchangeListing.id,
                ExchangeListing.status,
                ExchangeListing.yuvik_amount,
                ExchangeListing.want_description,
                ExchangeListing.item_type,
                ExchangeListing.seller_user_id,
                User.first_name.label("seller_name"),
                ExchangeListing.claimed_by_user_id,
                ExchangeListing.created_at,
                ExchangeListing.claimed_at,
                ExchangeListing.resolved_at,
            )
            .join(User, User.id == ExchangeListing.seller_user_id)
            .where(ExchangeListing.chat_id == chat_id, ExchangeListing.claimed_by_user_id == user_id)
            .order_by(ExchangeListing.created_at.desc())
        )
    ).all()

    result = [
        {
            "id": row.id,
            "role": "seller",
            "status": row.status,
            "yuvik_amount": row.yuvik_amount,
            "want_description": row.want_description,
            "item_type": row.item_type,
            "seller_user_id": row.seller_user_id,
            "seller_name": row.seller_name,
            "claimed_by_user_id": row.claimed_by_user_id,
            "claimed_by_name": row.claimed_by_name,
            "created_at": row.created_at,
            "claimed_at": row.claimed_at,
            "resolved_at": row.resolved_at,
        }
        for row in seller_rows
    ] + [
        {
            "id": row.id,
            "role": "buyer",
            "status": row.status,
            "yuvik_amount": row.yuvik_amount,
            "want_description": row.want_description,
            "item_type": row.item_type,
            "seller_user_id": row.seller_user_id,
            "seller_name": row.seller_name,
            "claimed_by_user_id": row.claimed_by_user_id,
            "claimed_by_name": None,
            "created_at": row.created_at,
            "claimed_at": row.claimed_at,
            "resolved_at": row.resolved_at,
        }
        for row in buyer_rows
    ]
    result.sort(key=lambda item: item["created_at"], reverse=True)
    return result
