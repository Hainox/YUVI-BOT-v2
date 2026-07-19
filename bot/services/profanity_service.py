"""Детектор мата на pymorphy3 + словарь лемм (AWARDS-01, номинация «матершинник»).

Чистая CPU-трансформация без БД: count_profanity(text) лемматизирует каждое
слово сообщения и считает, сколько лемм входит в словарь мата
(data/profanity_ru.txt) — инфлектированные формы («блядям», «блядью», «БЛЯДЬ»)
матчатся через лемматизацию, а не через regex/подстроку.

MorphAnalyzer грузит ~50 МБ словарь 2-5 сек, поэтому это module-level lazy
singleton — зеркалит паттерн bot/services/ai_client.py (module-level
AsyncOpenAI client, создаётся один раз на процесс). НИКОГДА не конструируется
per-call (Pitfall 6). init() — явный warm-up, вызывается из bot/main.py::run()
до dp.start_polling, чтобы холодный старт не падал на первое сообщение чата.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from pymorphy3 import MorphAnalyzer

logger = logging.getLogger(__name__)

# Путь к словарю лемм — та же схема резолва project root, что и
# backfill_service.py::_PROJECT_ROOT (bot/services/x.py -> parent.parent.parent).
_DICT_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "profanity_ru.txt"

# Только кириллица/латиница без цифр и пунктуации (форма из
# docs/refscan/old-yuvi.md §5).
_WORD_RE = re.compile(r"[а-яёa-z]+")

_analyzer: MorphAnalyzer | None = None


def _load_lemmas(path: Path) -> frozenset[str]:
    """Читает словарь лемм: одна лемма на строку, '#'-комментарии и пустые строки пропускаются."""
    lemmas: set[str] = set()
    with path.open("r", encoding="utf-8") as dict_file:
        for line in dict_file:
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            lemmas.add(stripped)
    return frozenset(lemmas)


# Словарь лемм — дешёвое чтение текстового файла, грузится один раз при
# импорте модуля (в отличие от MorphAnalyzer это не Pitfall 6 — не ~50MB).
_PROFANITY_LEMMAS: frozenset[str] = _load_lemmas(_DICT_PATH)


def _get_analyzer() -> MorphAnalyzer:
    """Lazy singleton MorphAnalyzer — НИКОГДА не конструируется per-call (Pitfall 6)."""
    global _analyzer
    if _analyzer is None:
        _analyzer = MorphAnalyzer(lang="ru")
    return _analyzer


def count_profanity(text: str | None) -> int:
    """Считает слова текста, чья лемма входит в словарь мата.

    Пустой/None/медиа-текст (нет caption) безопасно возвращает 0.
    """
    if not text:
        return 0

    analyzer = _get_analyzer()
    words = _WORD_RE.findall(text.lower())

    count = 0
    for word in words:
        normal_form = analyzer.parse(word)[0].normal_form
        if normal_form in _PROFANITY_LEMMAS:
            count += 1
    return count


def init() -> None:
    """Прогрев MorphAnalyzer (~50 МБ, 2-5 сек) до старта поллинга (Pitfall 6)."""
    _get_analyzer()
    logger.info(
        "profanity_service: MorphAnalyzer прогрет, словарь лемм — %d записей",
        len(_PROFANITY_LEMMAS),
    )
