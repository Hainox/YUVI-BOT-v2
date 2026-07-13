# Yuvi Bot v2

Yuvi Bot v2 — это новая, чисто спроектированная версия Telegram-бота для одного дружеского чата: сбор 100% активности, аналитика, AI-команды, игровая экономика и Mini App.

## Зачем этот репозиторий

Главная цель — **надежно собирать данные чата без потерь**, а поверх них строить:
- статистику и ежедневные игровые механики;
- AI-функции (summary, digest, ask, card);
- экономику «гривны» и игровые системы (рынки, гача, дуэли, казино).

## Технологический стек

| Блок | Стек |
|---|---|
| Bot | Python 3.11, aiogram 3.x |
| API | FastAPI |
| NLP | FastAPI + transformers (CPU) |
| Data | PostgreSQL + pgvector, Redis |
| ORM и миграции | SQLAlchemy 2 (async), Alembic |
| Mini App | SvelteKit + nginx |
| Деплой | docker-compose на VPS Ubuntu |

## Что уже подготовлено в репозитории

1. Базовая структура проекта (`bot`, `api`, `nlp`, `miniapp`, `common`, `migrations`).
2. Каркас `docker-compose.yml` для локального/серверного запуска.
3. Базовые Dockerfile для всех сервисов.
4. Минимальные рабочие точки входа:
   - бот с `/start` и `/health`;
   - API health endpoint;
   - NLP health endpoint.
5. Документация для старта:
   - `docs/deploy-vps-ubuntu.md`
   - `docs/botfather-setup.md`
   - `docs/roadmap.md`
6. `.env.example` со всеми ключевыми переменными.

## Архитектурный принцип v2

**Сначала сбор данных и целостность, потом фичи.**

Важные решения:
- сбор сообщений через middleware, чтобы команды не терялись;
- explicit `allowed_updates` с поддержкой реакций;
- append-only журнал экономики и идемпотентные денежные операции;
- все фоновые задачи через APScheduler + очередь задач в PostgreSQL;
- русский язык документации и простой стиль для входа в проект.

## Структура проекта

```text
Yuvi-Bot-v2/
├── bot/                 # Telegram bot (aiogram)
├── api/                 # FastAPI backend для Mini App
├── nlp/                 # FastAPI NLP сервис
├── miniapp/             # SvelteKit фронтенд
├── common/              # Общие db/models/helpers
├── migrations/          # Alembic
├── docs/                # Документация проекта
├── scripts/             # Вспомогательные скрипты
├── docker-compose.yml
├── .env.example
└── README.md
```

## Быстрый старт (локально)

1. Скопируй переменные окружения:
   ```bash
   cp .env.example .env
   ```
2. Заполни обязательные поля в `.env`:
   - `BOT_TOKEN`
   - `DATABASE_URL`
   - `REDIS_URL`
3. Запусти стек:
   ```bash
   docker compose up --build
   ```
4. Проверь:
   - API: `http://localhost:8002/health`
   - NLP: `http://localhost:8001/health`
   - Bot: команда `/health` в Telegram.

## Переменные окружения

Полный список: `.env.example`.

Минимум для старта:
- `BOT_TOKEN`
- `CHAT_ID`
- `DATABASE_URL`
- `REDIS_URL`
- `OPENAI_BASE_URL`
- `OPENAI_API_KEY`

## Roadmap разработки

Подробный план по фазам: `docs/roadmap.md`.

Коротко:
1. Ядро: сбор сообщений, реакции, статистика, базовые AI-команды.
2. Экономика: кошельки, банк, переводы, логи транзакций.
3. Игровые механики: рынки, гача, ферма, дуэли.
4. Mini App и визуальные игровые режимы.
5. Продовые полировки: мониторинг, backfill, админка, тесты.

## Обязательные настройки Telegram

Критично: **BotFather → Group Privacy = OFF**, иначе бот не увидит все сообщения и сбор статистики будет неполным.

Подробно: `docs/botfather-setup.md`.

## Для кого этот README

README написан специально простым языком: чтобы даже при минимальном опыте можно было:
- понять архитектуру;
- поднять проект;
- поэтапно развивать функциональность.

