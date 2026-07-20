"""Частотные словари слов и эмодзи (DATA-03).

Чистые функции extract_words/extract_emojis не трогают БД — юнит-тестируются
без Postgres. bump_word_frequency/bump_emoji_frequency инкрементально копят
счётчики через ON CONFLICT DO UPDATE (не перезапись, а +=).

extract_emojis использует emoji.emoji_list (Don't-Hand-Roll из RESEARCH.md) —
корректно ловит многокодепойнтные ZWJ-последовательности (👩‍🚀), в отличие
от ручного regex по Unicode-диапазонам.

Вызывается из CollectorMiddleware в той же транзакции, что и save_message.
"""

from __future__ import annotations

import re
from collections import Counter

import emoji
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from common.models.emoji_frequency import EmojiFrequency
from common.models.word_frequency import WordFrequency

_WORD_RE = re.compile(r"[а-яёa-z0-9]+", re.IGNORECASE)

# word_frequency.word — VARCHAR(128) (common/models/word_frequency.py). Токены
# длиннее лимита — не настоящие слова (спам вида "ааааааа..." на сотни
# символов без пробелов), а не что-то, что стоит обрезать/задваивать по
# случайному общему префиксу — просто не считаем их словом (в реальном чате
# встречается, ловит StringDataRightTruncationError при INSERT иначе).
_MAX_WORD_LEN = 128


def extract_words(text: str | None) -> list[str]:
    """Токенизирует текст на слова (кириллица/латиница/цифры), lowercased.

    Пустой/None текст -> []. Токены длиннее `_MAX_WORD_LEN` отбрасываются.
    """
    if not text:
        return []
    return [w.lower() for w in _WORD_RE.findall(text) if len(w) <= _MAX_WORD_LEN]


def extract_emojis(text: str | None) -> list[str]:
    """Извлекает все эмодзи из текста через emoji.emoji_list (не ручной regex).

    Пустой/None текст -> [].
    """
    if not text:
        return []
    return [item["emoji"] for item in emoji.emoji_list(text)]


async def bump_word_frequency(
    session: AsyncSession, chat_id: int, user_id: int, words: list[str]
) -> None:
    """Инкрементально увеличивает счётчики слов пользователя в чате.

    Пустой список -> ничего не делает. commit — на вызывающем.
    """
    if not words:
        return
    counts = Counter(words)
    rows = [
        {"chat_id": chat_id, "user_id": user_id, "word": word, "count": n}
        for word, n in counts.items()
    ]
    stmt = pg_insert(WordFrequency).values(rows)
    stmt = stmt.on_conflict_do_update(
        index_elements=["chat_id", "user_id", "word"],
        set_={"count": WordFrequency.count + stmt.excluded.count},
    )
    await session.execute(stmt)


async def bump_emoji_frequency(
    session: AsyncSession, chat_id: int, user_id: int, emojis: list[str]
) -> None:
    """Инкрементально увеличивает счётчики эмодзи пользователя в чате.

    Пустой список -> ничего не делает. commit — на вызывающем.
    """
    if not emojis:
        return
    counts = Counter(emojis)
    rows = [
        {"chat_id": chat_id, "user_id": user_id, "emoji": item, "count": n}
        for item, n in counts.items()
    ]
    stmt = pg_insert(EmojiFrequency).values(rows)
    stmt = stmt.on_conflict_do_update(
        index_elements=["chat_id", "user_id", "emoji"],
        set_={"count": EmojiFrequency.count + stmt.excluded.count},
    )
    await session.execute(stmt)
