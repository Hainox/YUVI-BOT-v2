"""SSE-эндпоинт живого баланса (D-02): `GET /api/v1/events`.

Транспорт — Redis Pub/Sub, канал `bal:{chat_id}` (общий на весь чат,
публикуется `bot/services/balance_events.py::publish_balance` строго
после `session.commit()`). `event_stream` фильтрует сообщения по
`user_id`, чтобы участник видел только свой баланс (T-04-09), и шлёт
heartbeat `": ping\n\n"` при простое — держит соединение живым через
nginx/прокси (Pitfall 2 из 04-RESEARCH.md).

Ручной `StreamingResponse` вместо `fastapi.sse.EventSourceResponse` —
та фича появилась только в FastAPI >=0.135, проект пинует 0.116.1.

Роут защищён `Depends(require_membership)` (D-01, из 04-02); `initData`
приходит через query-параметр `init_data`, т.к. браузерный `EventSource`
не умеет кастомные заголовки (Pitfall 5).
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from collections.abc import Awaitable
from collections.abc import Callable

from fastapi import APIRouter
from fastapi import Depends
from fastapi import Request
from fastapi.responses import StreamingResponse

from api.deps import require_membership

router = APIRouter()


async def event_stream(
    redis_client,
    chat_id: int,
    user_id: int,
    is_disconnected: Callable[[], Awaitable[bool]],
) -> AsyncIterator[str]:
    """Async-генератор SSE-сообщений канала `bal:{chat_id}`, отфильтрованных
    по `user_id`. Принимает `is_disconnected` отдельным параметром (не
    `Request`), чтобы быть тестируемым без реального FastAPI Request.

    Best-effort: любая ошибка Redis (подписка не удалась, соединение
    оборвалось) — генератор просто завершается, без исключения наружу
    (D-02 — деградирует только live-UI, не деньги).
    """
    channel = f"bal:{chat_id}"
    try:
        async with redis_client.pubsub() as pubsub:
            await pubsub.subscribe(channel)
            while True:
                if await is_disconnected():
                    break
                message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=20.0)
                if message is None:
                    yield ": ping\n\n"
                    continue
                payload = json.loads(message["data"])
                if payload.get("user_id") != user_id:
                    continue
                yield f"data: {json.dumps(payload)}\n\n"
    except Exception:
        return


@router.get("/api/v1/events")
async def sse_events(request: Request, auth=Depends(require_membership)) -> StreamingResponse:
    return StreamingResponse(
        event_stream(request.app.state.redis, auth.chat_id, auth.user_id, request.is_disconnected),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
