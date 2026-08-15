"""Тесты retry/fallback-по-моделям (`ai_client.complete_with_fallback`,
тех.долг "надёжность AI-фич", запрошено 2026-07-29).

Доказывают:
- Успех с первой попытки не трогает остальной каталог моделей.
- AIEmptyResponseError/таймаут/сеть/rate-limit/5xx/модель-не-найдена
  переключают на следующую модель каталога (primary_model — первым).
- AuthenticationError/BadRequestError (и прочие APIStatusError вне списка
  триггеров) НЕ переключают модель — распространяются немедленно.
- Если отказали ВСЕ модели каталога — наружу летит последняя пойманная
  ошибка (не проглатывается).
- get_failure_counts() считает отказы по каждой отказавшей модели.
- available_models()/_model_fallback_order() парсят каталог и ставят
  primary_model первым независимо от его позиции в settings.ai_available_models.

Мокаем ai_client.stream той же формой async-generator'а, что test_twin_service.py/
test_card_service.py — реального похода к OpenCode Go нет.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import openai
import pytest

from bot.config import settings
from bot.services import ai_client


@pytest.fixture(autouse=True)
def _reset_failure_counts():
    """_failure_counts — модульный singleton-счётчик, живущий весь процесс
    (специально, для /model_health) — тесты обязаны не делиться состоянием."""
    ai_client._failure_counts.clear()
    yield
    ai_client._failure_counts.clear()


def _make_stream(behaviors: dict[str, list[str] | Exception]):
    """behaviors[model] — либо список чанков для yield, либо исключение
    (экземпляр) для raise. Модель, не упомянутая в behaviors, вызывает
    KeyError — тест обязан явно описать поведение каждой ожидаемо
    вызываемой модели."""

    async def _stream(messages: list[dict], model: str, max_tokens: int) -> AsyncIterator[str]:
        outcome = behaviors[model]
        if isinstance(outcome, Exception):
            raise outcome
        for chunk in outcome:
            yield chunk

    return _stream


def _status_error(cls: type[openai.APIStatusError], status_code: int, message: str = "boom"):
    request = httpx.Request("POST", "https://example.test/v1/chat/completions")
    response = httpx.Response(status_code, request=request)
    return cls(message, response=response, body=None)


def _timeout_error() -> openai.APITimeoutError:
    request = httpx.Request("POST", "https://example.test/v1/chat/completions")
    return openai.APITimeoutError(request=request)


def _connection_error() -> openai.APIConnectionError:
    request = httpx.Request("POST", "https://example.test/v1/chat/completions")
    return openai.APIConnectionError(request=request)


# --- available_models / _model_fallback_order --------------------------------


def test_available_models_parses_catalog(monkeypatch):
    monkeypatch.setattr(settings, "ai_available_models", " model-a, model-b ,model-c")
    assert ai_client.available_models() == ["model-a", "model-b", "model-c"]


def test_fallback_order_puts_primary_first_regardless_of_catalog_position(monkeypatch):
    monkeypatch.setattr(settings, "ai_available_models", "model-a,model-b,model-c")
    assert ai_client._model_fallback_order("model-c") == ["model-c", "model-a", "model-b"]


def test_fallback_order_dedupes_primary_if_already_in_catalog(monkeypatch):
    monkeypatch.setattr(settings, "ai_available_models", "model-a,model-b")
    order = ai_client._model_fallback_order("model-a")
    assert order == ["model-a", "model-b"]
    assert order.count("model-a") == 1


# --- complete_with_fallback: успех с первой попытки ---------------------------


@pytest.mark.asyncio
async def test_succeeds_first_try_does_not_touch_rest_of_catalog(monkeypatch):
    calls: list[str] = []

    async def _stream(messages, model, max_tokens):
        calls.append(model)
        yield "ок"

    monkeypatch.setattr(ai_client, "stream", _stream)
    monkeypatch.setattr(settings, "ai_available_models", "model-a,model-b,model-c")

    result = await ai_client.complete_with_fallback(
        [{"role": "user", "content": "привет"}], primary_model="model-a", max_tokens=100
    )

    assert result == "ок"
    assert calls == ["model-a"]
    assert ai_client.get_failure_counts() == {}


# --- fallback-триггеры: AIEmptyResponseError и сетевые/5xx --------------------


@pytest.mark.asyncio
async def test_falls_back_on_empty_response_error(monkeypatch):
    monkeypatch.setattr(settings, "ai_available_models", "model-a,model-b")
    monkeypatch.setattr(
        ai_client,
        "stream",
        _make_stream({"model-a": ai_client.AIEmptyResponseError("model-a"), "model-b": ["привет", " мир"]}),
    )

    result = await ai_client.complete_with_fallback(
        [{"role": "user", "content": "?"}], primary_model="model-a", max_tokens=100
    )

    assert result == "привет мир"
    assert ai_client.get_failure_counts() == {"model-a": 1}


@pytest.mark.parametrize(
    "make_error",
    [
        _timeout_error,
        _connection_error,
        lambda: _status_error(openai.RateLimitError, 429),
        lambda: _status_error(openai.InternalServerError, 500),
        lambda: _status_error(openai.NotFoundError, 404),
    ],
    ids=["timeout", "connection", "rate_limit", "internal_server", "not_found"],
)
@pytest.mark.asyncio
async def test_falls_back_on_transient_network_and_status_errors(monkeypatch, make_error):
    monkeypatch.setattr(settings, "ai_available_models", "model-a,model-b")
    monkeypatch.setattr(
        ai_client,
        "stream",
        _make_stream({"model-a": make_error(), "model-b": ["выжил"]}),
    )

    result = await ai_client.complete_with_fallback(
        [{"role": "user", "content": "?"}], primary_model="model-a", max_tokens=100
    )

    assert result == "выжил"
    assert ai_client.get_failure_counts() == {"model-a": 1}


# --- НЕ fallback-триггеры: авторизация/валидация запроса ---------------------


@pytest.mark.parametrize(
    "make_error",
    [
        lambda: _status_error(openai.AuthenticationError, 401),
        lambda: _status_error(openai.BadRequestError, 400),
        lambda: _status_error(openai.PermissionDeniedError, 403),
    ],
    ids=["authentication", "bad_request", "permission_denied"],
)
@pytest.mark.asyncio
async def test_does_not_fall_back_on_non_model_specific_errors(monkeypatch, make_error):
    """Ошибки одинаковые для любой модели этого же гейтвея — ретрай другой
    моделью не поможет и спрятал бы реальный баг вызывающего кода."""
    calls: list[str] = []
    error = make_error()

    async def _stream(messages, model, max_tokens):
        calls.append(model)
        raise error
        yield  # pragma: no cover - unreachable, делает функцию async-генератором

    monkeypatch.setattr(ai_client, "stream", _stream)
    monkeypatch.setattr(settings, "ai_available_models", "model-a,model-b,model-c")

    with pytest.raises(type(error)):
        await ai_client.complete_with_fallback(
            [{"role": "user", "content": "?"}], primary_model="model-a", max_tokens=100
        )

    assert calls == ["model-a"]  # ни одна другая модель не была вызвана
    assert ai_client.get_failure_counts() == {}  # не считается отказом fallback-инфры


# --- исчерпание всего каталога ------------------------------------------------


@pytest.mark.asyncio
async def test_exhausting_whole_catalog_reraises_last_error(monkeypatch):
    monkeypatch.setattr(settings, "ai_available_models", "model-a,model-b,model-c")
    last_error = ai_client.AIEmptyResponseError("model-c")
    monkeypatch.setattr(
        ai_client,
        "stream",
        _make_stream(
            {
                "model-a": ai_client.AIEmptyResponseError("model-a"),
                "model-b": ai_client.AIEmptyResponseError("model-b"),
                "model-c": last_error,
            }
        ),
    )

    with pytest.raises(ai_client.AIEmptyResponseError) as exc_info:
        await ai_client.complete_with_fallback(
            [{"role": "user", "content": "?"}], primary_model="model-a", max_tokens=100
        )

    assert exc_info.value is last_error
    assert ai_client.get_failure_counts() == {"model-a": 1, "model-b": 1, "model-c": 1}


# --- AIEmptyResponseError несёт model ------------------------------------------


def test_ai_empty_response_error_carries_model():
    exc = ai_client.AIEmptyResponseError("grok-4.5")
    assert exc.model == "grok-4.5"
    assert "grok-4.5" in str(exc)
