"""Best-effort публикация живого баланса в Redis (D-02).

`publish_balance` вызывается вызывающим кодом СТРОГО ПОСЛЕ
`session.commit()` — до commit публиковать нельзя: это гонка, при которой
клиент увидел бы баланс, который может ещё откатиться (IntegrityError на
повторном ref_id, откат транзакции и т.п.).

Публикация — best-effort: любое исключение при обращении к Redis (сеть,
таймаут, Redis не поднят) глушится и логируется на уровне debug. Redis
используется только для live-обновления Mini App через SSE
(`api/routes/events.py`); деньги проводятся через `economy_service` +
Postgres независимо от Redis, поэтому отказ Redis никогда не должен
ронять денежный флоу (D-02 философия — «деградирует только live-UI»).
"""

from __future__ import annotations

import json
import logging

logger = logging.getLogger(__name__)


async def publish_balance(redis_client, chat_id: int, user_id: int, balance: int) -> None:
    """Публикует {"user_id", "balance"} в канал `bal:{chat_id}`.

    Вызывать строго после `session.commit()`. `redis_client is None`
    (Redis не настроен, см. `api/main.py::lifespan`) — ранний no-op.
    """
    if redis_client is None:
        return
    try:
        payload = json.dumps({"user_id": user_id, "balance": balance})
        await redis_client.publish(f"bal:{chat_id}", payload)
    except Exception:
        logger.debug("publish_balance: Redis недоступен, live-обновление пропущено", exc_info=True)
