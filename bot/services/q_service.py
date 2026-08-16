"""Поиск по истории чата и ответ на `/q`.

Пайплайн намеренно разделён на дешёвые локальные операции и два LLM-вызова:
1. LLM возвращает до трёх разговорных переформулировок вопроса;
2. оригинал и переформулировки эмбеддятся локальным BGE-M3;
3. каждый вектор ищется отдельно, затем хиты объединяются по максимальной
   cosine-sхожести;
4. поверх векторов добавляется простой ILIKE-слой со стеммингом и скоупом
   автора по `@username`;
5. к каждому хиту добавляются два сообщения до и после;
6. второй LLM-вызов получает хронологический контекст и отвечает по существу.

LLM-провайдер остаётся OpenAI-совместимым: в текущем проекте это OpenCode Go,
а при смене OPENAI_BASE_URL тот же код работает с xAI/OpenRouter. Локальная
часть не вызывает внешний API.
"""

from __future__ import annotations

import re
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import and_
from sqlalchemy import or_
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import EXPLICIT_LANGUAGE_INSTRUCTION
from bot.config import settings
from bot.services import ai_client
from bot.services import nlp_client
from bot.services import settings_service
from common.models.message import Message
from common.models.message_embedding import MessageEmbedding
from common.models.user import User

MSK = ZoneInfo("Europe/Moscow")

_STOP_WORDS = {
    "этот", "эта", "это", "эти", "того", "тоже", "если", "чтобы", "когда", "где",
    "есть", "был", "была", "были", "быть", "или", "что", "как", "кто", "чем", "про",
    "для", "после", "перед", "под", "над", "при", "без", "через", "мне", "тебе", "ему",
    "она", "они", "мы", "вы", "все", "всё", "так", "уже", "ещё", "еще", "очень", "сам",
    "сама", "сами", "можно", "нужно", "тут", "там", "его", "ее", "их", "наш", "ваш",
}
_WORD_RE = re.compile(r"[A-Za-zА-Яа-яЁё]{4,}")
_AUTHOR_RE = re.compile(r"@([A-Za-z0-9_]{3,32})")

Q_REWRITE_SYSTEM_PROMPT = (
    "Ты помогаешь искать сообщения в русскоязычном групповом чате. "
    "Переформулируй вопрос пользователя в три коротких разговорных варианта "
    "с тем же смыслом, но с другими словами и возможными формами, в которых "
    "это могло быть сказано в чате. Верни ровно три варианта, по одному на строке, "
    "без нумерации, кавычек и пояснений."
)

Q_ANSWER_SYSTEM_PROMPT = """Ты отвечаешь участнику русскоязычного group chat. Тебе дают его запрос и фрагменты истории этого чата, найденные поиском.
Правила:
- Отвечай на запрос по существу. Это не поиск по истории: если фрагменты к делу не относятся, отвечай сам, а не пиши «в чате этого нет».
- Фрагменты — знание о людях, событиях и манере общения чата. Опирайся на них, когда они по делу, и держись их фактов: не приписывай участникам того, чего они не говорили.
- Сленг и мат сохраняй, стиль чата держи. Не цензурируй и не морализируй.
- Без эмодзи, без вступлений вроде «конечно» и «отличный вопрос».
- Explicit-выражения, мат и грубую лексику можно сохранять, если они уместны стилю чата; не заменяй их эвфемизмами.
"""


def _term_root(word: str) -> str:
    """Грубый русский/латинский корень для устойчивого ILIKE-поиска."""
    word = word.lower()
    return word[:-2] if len(word) > 6 else word


def _keyword_terms(query: str) -> list[str]:
    terms: list[str] = []
    for word in _WORD_RE.findall(query.lower()):
        if word in _STOP_WORDS:
            continue
        root = _term_root(word)
        if root not in terms:
            terms.append(root)
    return terms[:6]


def _extract_author(query: str) -> str | None:
    match = _AUTHOR_RE.search(query)
    return match.group(1).lower() if match else None


def _clean_rewrite(value: str) -> str:
    value = re.sub(r"^\s*(?:[-*•]|\d+[.)])\s*", "", value)
    return value.strip().strip('"“”«»')


async def _rewrite_query(session: AsyncSession, chat_id: int, question: str) -> list[str]:
    """Получает три варианта; при сбое безопасно возвращает пустой список."""
    model = await settings_service.get_active_model(
        session, chat_id, default=settings.ai_q_model
    )
    try:
        raw = await ai_client.complete_with_fallback(
            [
                {"role": "system", "content": Q_REWRITE_SYSTEM_PROMPT},
                {"role": "user", "content": question},
            ],
            primary_model=model,
            max_tokens=settings.ai_q_rewrite_max_output_tokens,
        )
    except Exception:  # noqa: BLE001 - поиск продолжает работать по оригиналу
        return []

    variants: list[str] = []
    for line in raw.splitlines():
        cleaned = _clean_rewrite(line)
        if cleaned and cleaned.casefold() != question.casefold() and cleaned not in variants:
            variants.append(cleaned[: settings.ai_q_max_query_chars])
    return variants[: settings.ai_q_rewrite_variants]


async def _vector_search(
    session: AsyncSession,
    chat_id: int,
    embedding: list[float],
    top_k: int,
) -> list[dict]:
    distance = MessageEmbedding.embedding.cosine_distance(embedding)
    rows = (
        await session.execute(
            select(
                Message.id,
                Message.text,
                Message.created_at,
                User.username,
                User.first_name,
                distance.label("distance"),
            )
            .join(MessageEmbedding, MessageEmbedding.message_id == Message.id)
            .outerjoin(User, User.id == Message.user_id)
            .where(MessageEmbedding.chat_id == chat_id, Message.text.is_not(None))
            .order_by(distance)
            .limit(top_k)
        )
    ).all()
    return [
        {
            "id": row.id,
            "text": row.text,
            "created_at": row.created_at,
            "username": row.username,
            "first_name": row.first_name,
            "similarity": max(-1.0, min(1.0, 1.0 - float(row.distance))),
        }
        for row in rows
    ]


async def _lexical_search(
    session: AsyncSession,
    chat_id: int,
    query: str,
    top_k: int,
) -> list[dict]:
    terms = _keyword_terms(query)
    if not terms:
        return []

    author = _extract_author(query)
    text_lower = Message.text
    matches = [text_lower.ilike(f"%{term}%") for term in terms]
    stmt = (
        select(Message.id, Message.text, Message.created_at, User.username, User.first_name)
        .outerjoin(User, User.id == Message.user_id)
        .where(Message.chat_id == chat_id, Message.text.is_not(None), or_(*matches))
        .order_by(Message.created_at, Message.id)
        .limit(top_k * 3 if author else top_k)
    )
    if author:
        stmt = stmt.where(User.username.is_not(None), User.username.ilike(author))

    rows = (await session.execute(stmt)).all()
    result: list[dict] = []
    for row in rows:
        lowered = (row.text or "").lower()
        matched = sum(term in lowered for term in terms)
        similarity = (0.97 if author else 0.80) + 0.01 * matched
        result.append(
            {
                "id": row.id,
                "text": row.text,
                "created_at": row.created_at,
                "username": row.username,
                "first_name": row.first_name,
                "similarity": similarity,
            }
        )
    return result


async def hybrid_search(
    session: AsyncSession,
    chat_id: int,
    query_embeddings: list[list[float]],
    query_text: str,
) -> list[dict]:
    """Ищет по каждому вектору и объединяет результат с ILIKE-слоем по max(similarity)."""
    vector_lists = [
        await _vector_search(session, chat_id, embedding, settings.ai_q_per_query_k)
        for embedding in query_embeddings
    ]
    lexical = await _lexical_search(session, chat_id, query_text, settings.ai_q_top_k)

    merged: dict[int, dict] = {}
    for row in [item for rows in vector_lists for item in rows] + lexical:
        current = merged.get(row["id"])
        if current is None or row["similarity"] > current["similarity"]:
            merged[row["id"]] = row
    return sorted(merged.values(), key=lambda row: (-row["similarity"], row["id"]))[: settings.ai_q_top_k]


async def _neighbors(
    session: AsyncSession,
    chat_id: int,
    hit: dict,
    before: bool,
) -> list[dict]:
    timestamp = hit["created_at"]
    if timestamp is None:
        return []
    ordering = (
        (Message.created_at.desc(), Message.id.desc())
        if before
        else (Message.created_at, Message.id)
    )
    if before:
        condition = or_(
            Message.created_at < timestamp,
            and_(Message.created_at == timestamp, Message.id < hit["id"]),
        )
    else:
        condition = or_(
            Message.created_at > timestamp,
            and_(Message.created_at == timestamp, Message.id > hit["id"]),
        )
    rows = (
        await session.execute(
            select(Message.id, Message.text, Message.created_at, User.username, User.first_name)
            .outerjoin(User, User.id == Message.user_id)
            .where(
                Message.chat_id == chat_id,
                Message.text.is_not(None),
                condition,
            )
            .order_by(*ordering)
            .limit(settings.ai_q_neighbors_each_side)
        )
    ).all()
    if before:
        rows = list(reversed(rows))
    return [
        {
            "id": row.id,
            "text": row.text,
            "created_at": row.created_at,
            "username": row.username,
            "first_name": row.first_name,
            "similarity": None,
            "is_hit": False,
        }
        for row in rows
    ]


async def _expand_with_neighbors(
    session: AsyncSession, chat_id: int, hits: list[dict]
) -> list[dict]:
    expanded: dict[int, dict] = {}
    for hit in hits:
        expanded[hit["id"]] = {**hit, "is_hit": True}
        for neighbor in await _neighbors(session, chat_id, hit, before=True):
            expanded.setdefault(neighbor["id"], neighbor)
        for neighbor in await _neighbors(session, chat_id, hit, before=False):
            expanded.setdefault(neighbor["id"], neighbor)
    return sorted(
        expanded.values(),
        key=lambda row: (row["created_at"] or datetime.min, row["id"]),
    )


def _format_datetime(value: datetime | None) -> str:
    if value is None:
        return "неизвестное время"
    if value.tzinfo is None:
        value = value.replace(tzinfo=ZoneInfo("UTC"))
    return value.astimezone(MSK).strftime("%Y-%m-%d %H:%M МСК")


def _format_context(rows: list[dict]) -> str:
    lines: list[str] = []
    for row in rows:
        marker = "★" if row["is_hit"] else "·"
        score = f"[sim={row['similarity']:.2f}] " if row["is_hit"] else ""
        author = f"@{row['username']}" if row.get("username") else row.get("first_name") or "Аноним"
        text = (row.get("text") or "").replace("\x00", " ").strip()[: settings.ai_q_max_message_chars]
        lines.append(f"{marker}{score}{_format_datetime(row.get('created_at'))} {author}: {text}")
    return "\n".join(lines)


async def answer(session: AsyncSession, chat_id: int, question: str) -> str:
    """Выполняет полный `/q`-поток и возвращает собранный ответ без стриминга."""
    question = question.strip()[: settings.ai_q_max_query_chars]
    variants = await _rewrite_query(session, chat_id, question)
    queries = [question, *variants]
    embeddings = await nlp_client.embed_batch(queries)
    hits = await hybrid_search(session, chat_id, embeddings, question)
    context_rows = await _expand_with_neighbors(session, chat_id, hits)

    context = _format_context(context_rows)
    if len(context) > settings.ai_q_max_context_chars:
        context = context[: settings.ai_q_max_context_chars] + "\n[Контекст обрезан по лимиту. ]"
    user_prompt = (
        f"ВОПРОС ПОЛЬЗОВАТЕЛЯ: {question}\n"
        f"НАЙДЕННЫЕ СООБЩЕНИЯ (★ релевантных: {len(hits)}, плюс по ±2 соседних; "
        f"всего: {len(context_rows)}):\n{context or '[релевантных сообщений не найдено]'}"
    )
    model = await settings_service.get_active_model(
        session, chat_id, default=settings.ai_q_model
    )
    system_prompt = Q_ANSWER_SYSTEM_PROMPT
    if await settings_service.get_explicit_language_enabled(session, chat_id):
        system_prompt += f"\n{EXPLICIT_LANGUAGE_INSTRUCTION}"
    return await ai_client.complete_with_fallback(
        [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        primary_model=model,
        max_tokens=settings.ai_max_output_tokens,
    )
