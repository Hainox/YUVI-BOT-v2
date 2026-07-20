"""Automated-гейт DEPLOY-03: каждый `Field(alias=...)` в `bot.config.Settings`
обязан присутствовать в `.env.example`. Без этого гейта забытая при
добавлении новая переменная окружения молча ломает деплой на чистом VPS —
новичок скопирует `.env.example` в `.env`, а бот упадёт на старте из-за
отсутствующего значения, о котором никто не предупредил.

Чистое чтение файлов — Postgres не требуется, в отличие от большинства
тестов проекта (сравни с фикстурой `session` в tests/conftest.py).
"""

from __future__ import annotations

from pathlib import Path

from bot.config import Settings

ENV_EXAMPLE_PATH = Path(__file__).resolve().parent.parent / ".env.example"


def _env_example_keys() -> set[str]:
    """Ключи вида `ALIAS=...` из .env.example (не считая закомментированных строк)."""
    keys: set[str] = set()
    for line in ENV_EXAMPLE_PATH.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key = stripped.split("=", 1)[0].strip()
        keys.add(key)
    return keys


def _settings_aliases() -> set[str]:
    """Все alias'ы Field(...) из bot.config.Settings (источник истины)."""
    return {
        field.alias
        for field in Settings.model_fields.values()
        if field.alias
    }


def test_env_example_file_exists_and_not_empty() -> None:
    assert ENV_EXAMPLE_PATH.exists(), ".env.example отсутствует в корне проекта"
    assert ENV_EXAMPLE_PATH.stat().st_size > 0, ".env.example пустой"


def test_env_example_has_every_settings_alias() -> None:
    """DEPLOY-03: ни один alias Settings не должен быть забыт в .env.example."""
    env_keys = _env_example_keys()
    aliases = _settings_aliases()

    missing = aliases - env_keys
    assert not missing, (
        "Забытые переменные в .env.example (есть в bot.config.Settings, "
        f"но нет в .env.example): {sorted(missing)}"
    )
