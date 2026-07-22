"""Тесты bot/services/frequency_service.py.

Юнит-тесты extract_words/extract_emojis — чистые функции, без БД.
Интеграционный тест bump_word_frequency/bump_emoji_frequency — против живого
Postgres (фикстура `session` из conftest.py), доказывает инкрементальный
upsert (повторный bump суммирует count, а не перезаписывает).
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from bot.services import frequency_service
from common.models.emoji_frequency import EmojiFrequency
from common.models.user import User
from common.models.word_frequency import WordFrequency


# --- extract_words -----------------------------------------------------


def test_extract_words_lowercases_cyrillic_and_latin_and_digits():
    words = frequency_service.extract_words("Привет Mir123 ПРИВЕТ")
    assert words == ["привет", "mir123", "привет"]


def test_extract_words_empty_string_returns_empty_list():
    assert frequency_service.extract_words("") == []


def test_extract_words_none_returns_empty_list():
    assert frequency_service.extract_words(None) == []


def test_extract_words_ignores_punctuation_and_emoji():
    words = frequency_service.extract_words("привет, мир! 🔥🔥")
    assert words == ["привет", "мир"]


# --- extract_emojis ------------------------------------------------------


def test_extract_emojis_counts_zwj_sequence_as_one_item():
    # 👩‍🚀 — женщина-космонавт, ZWJ-последовательность из 3 кодпойнтов —
    # должна считаться ОДНИМ эмодзи, не тремя.
    emojis = frequency_service.extract_emojis("привет 👩‍🚀 мир")
    assert emojis == ["👩‍🚀"]


def test_extract_emojis_multiple_simple_emojis():
    emojis = frequency_service.extract_emojis("😀 текст 🔥🔥")
    assert emojis == ["😀", "🔥", "🔥"]


def test_extract_emojis_empty_string_returns_empty_list():
    assert frequency_service.extract_emojis("") == []


def test_extract_emojis_none_returns_empty_list():
    assert frequency_service.extract_emojis(None) == []


def test_extract_emojis_no_emoji_in_text_returns_empty_list():
    assert frequency_service.extract_emojis("просто текст без эмодзи") == []


def test_extract_emojis_handles_kurigram_str_subclass():
    # Regression: message.text/caption из Kurigram — не plain str, а подкласс
    # Str с UTF-16-aware __getitem__ (для offset'ов Telegram-сущностей). На
    # ZWJ-последовательностях это роняло UnicodeDecodeError внутри
    # emoji.emoji_list при реальном backfill (chat_id=-1002586380924).
    from pyrogram.types.messages_and_media.message import Str

    emojis = frequency_service.extract_emojis(Str("привет 👩‍🚀 мир"))
    assert emojis == ["👩‍🚀"]


# --- bump_word_frequency / bump_emoji_frequency (интеграционные) ---------


@pytest.mark.asyncio
async def test_bump_word_frequency_inserts_and_increments_on_repeat(session):
    chat_id = -100200300400
    user_id = 700100200
    await session.execute(
        User.__table__.insert().values(id=user_id, username=None, first_name="Тест")
    )

    await frequency_service.bump_word_frequency(session, chat_id, user_id, ["привет", "привет", "мир"])
    await session.flush()

    rows = (
        await session.execute(
            select(WordFrequency).where(
                WordFrequency.chat_id == chat_id, WordFrequency.user_id == user_id
            )
        )
    ).scalars().all()
    by_word = {r.word: r.count for r in rows}
    assert by_word == {"привет": 2, "мир": 1}

    # Повторный bump тех же слов -> счётчик СУММИРУЕТСЯ (инкремент, не перезапись).
    await frequency_service.bump_word_frequency(session, chat_id, user_id, ["привет"])
    await session.flush()
    # bump_word_frequency пишет через Core (on_conflict_do_update), минуя ORM
    # unit-of-work — уже загруженные в identity map объекты не обновляются сами
    # по себе, нужен явный expire перед повторным select.
    session.expire_all()

    rows_after = (
        await session.execute(
            select(WordFrequency).where(
                WordFrequency.chat_id == chat_id,
                WordFrequency.user_id == user_id,
                WordFrequency.word == "привет",
            )
        )
    ).scalar_one()
    assert rows_after.count == 3


@pytest.mark.asyncio
async def test_bump_word_frequency_empty_list_is_noop(session):
    chat_id = -100200300401
    user_id = 700100201
    await session.execute(
        User.__table__.insert().values(id=user_id, username=None, first_name="Тест")
    )

    await frequency_service.bump_word_frequency(session, chat_id, user_id, [])
    await session.flush()

    rows = (
        await session.execute(
            select(WordFrequency).where(
                WordFrequency.chat_id == chat_id, WordFrequency.user_id == user_id
            )
        )
    ).scalars().all()
    assert rows == []


@pytest.mark.asyncio
async def test_bump_emoji_frequency_inserts_and_increments_on_repeat(session):
    chat_id = -100200300402
    user_id = 700100202
    await session.execute(
        User.__table__.insert().values(id=user_id, username=None, first_name="Тест")
    )

    await frequency_service.bump_emoji_frequency(session, chat_id, user_id, ["🔥", "🔥", "😀"])
    await session.flush()

    rows = (
        await session.execute(
            select(EmojiFrequency).where(
                EmojiFrequency.chat_id == chat_id, EmojiFrequency.user_id == user_id
            )
        )
    ).scalars().all()
    by_emoji = {r.emoji: r.count for r in rows}
    assert by_emoji == {"🔥": 2, "😀": 1}

    # Повторный bump -> инкремент, не перезапись.
    await frequency_service.bump_emoji_frequency(session, chat_id, user_id, ["🔥"])
    await session.flush()
    session.expire_all()

    row_after = (
        await session.execute(
            select(EmojiFrequency).where(
                EmojiFrequency.chat_id == chat_id,
                EmojiFrequency.user_id == user_id,
                EmojiFrequency.emoji == "🔥",
            )
        )
    ).scalar_one()
    assert row_after.count == 3
