"""Telegram Stars (XTR) донаты → ювики (STARS-01, D-09/D-11/D-12).

Деньги идут ТОЛЬКО через `economy_service` — этот модуль не пишет
`user_balance`/`economy_tx` напрямую, а лишь строит параметры инвойса и
переиспользует уже проверенный идемпотентный примитив `economy_service.credit`
(RESEARCH.md Code Examples, verified через Context7 aiogram 3.27).
"""

from __future__ import annotations

from aiogram.types import LabeledPrice
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import settings
from bot.services import economy_service

INVOICE_TITLE = "Донат Ювикам"
KIND = "stars_donate"


def build_invoice_kwargs(stars: int, user_id: int, rate: int) -> dict:
    """Параметры для `bot.send_invoice` — XTR-инвойс на `stars` звёзд.

    `currency="XTR"` и `provider_token=""` обязательны для Telegram Stars;
    ровно один `LabeledPrice` с `amount=stars` (для XTR — целое число звёзд,
    не копейки/exp=0)."""
    return {
        "title": INVOICE_TITLE,
        "description": f"{stars}⭐ = {stars * rate} ювиков",
        "payload": f"stars_donate:{user_id}",
        "currency": "XTR",
        "prices": [LabeledPrice(label="Донат", amount=stars)],
        "provider_token": "",
    }


async def credit_from_payment(
    session: AsyncSession, chat_id: int, user_id: int, stars: int, charge_id: str
) -> bool:
    """Идемпотентное начисление ювиков за донат звёздами (D-09: курс из
    `settings.stars_to_juvik_rate`). `ref_id=f"stars:{charge_id}"` — повтор
    того же `charge_id` (реконнект polling) возвращает False, деньги не
    начисляются повторно. Не коммитит — транзакцию завершает вызывающий
    (`bot/handlers/donate.py::on_successful_payment`)."""
    juviks = stars * settings.stars_to_juvik_rate
    return await economy_service.credit(
        session, chat_id, user_id, juviks, kind=KIND, ref_id=f"stars:{charge_id}"
    )
