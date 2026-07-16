# Отчёт: инфраструктура и конфигурация эталона xyloz_tg_bot

> Зона анализа: docker-compose, Dockerfiles, конфигурация (env), логирование, APScheduler, Alembic, scripts/, деплой (Dokploy), nginx для Mini App, настройка BotFather.
>
> Источник: репозиторий `Heide172/xyloz_tg_bot` (публичный GitHub, «Телеграм бот для сбора и анализа сообщений»). Локальная копия для анализа: `C:\Users\root\AppData\Local\Temp\claude\C--Users-root-Desktop-YUVI-BOT\327a8fc2-b8d8-4c65-9d50-2d3d3ac0d69d\scratchpad\xyloz_tg_bot`.
>
> ВАЖНО: в текущей версии репозитория VPN-сервисов в docker-compose НЕТ. Единственный «прокси-след» — закомментированная переменная `API_EXTERNAL_PROXY` у сервиса cobalt (обход датацентр-блокировок). В Yuvi Bot v2 всё это по требованию исключается — помечено ниже.

---

## 1. Структура репозитория (верхний уровень)

```
xyloz_tg_bot/
├── docker-compose.yml        # 6 сервисов: migrations, redis, bot, nlp, api, miniapp, cobalt
├── bot.Dockerfile            # общий образ для bot и migrations
├── alembic.ini               # script_location = %(here)s/migrations
├── requirements.txt          # корневые зависимости (bot + migrations)
├── bot/                      # aiogram 3: main.py, handlers/ (16 шт), services/ (34 шт)
├── api/                      # FastAPI backend Mini App: Dockerfile, main.py, auth.py, routes/ (16 шт)
├── nlp/                      # FastAPI NLP-сервис: Dockerfile, main.py (sentiment/toxicity/embeddings)
├── miniapp/                  # SvelteKit SPA: Dockerfile (node build → nginx), nginx.conf
├── cobalt/                   # форк cobalt: Dockerfile + patched/*.js (поле caption)
├── worker/                   # ЗАГОТОВКА RQ (main.py ~15 строк), в compose НЕ подключён
├── common/                   # db/, models/, logger/, prompts.py, events.py, metrics.py
├── config/prompts/           # 14 md-промптов (ask, digest, joke, phrase, summary, user_card)
├── migrations/               # alembic: env.py + versions/ (16 ревизий)
├── scripts/                  # 19 скриптов: db_*, backfill'ы, history_load, pgvector, cover
└── docs/gacha_v2.md          # единственный doc (не инфра)
```

Ключевой факт: **никакого каталога infra/, k8s/ и т.п. нет** (README описывает «идеальную» структуру, реальность проще). Конфигурация — чистые env-переменные без pydantic-settings.

---

## 2. docker-compose.yml — все сервисы

Файл: `docker-compose.yml` (version "3.9"). Один compose на весь стек.

### 2.1 Сводная таблица сервисов

| Сервис | Образ / build | Порты (host:cont) | depends_on | healthcheck | Сети | Volumes |
|---|---|---|---|---|---|---|
| `migrations` | build: `bot.Dockerfile`, context `.` | — | — | — | default + dokploy-network | — |
| `redis` | `redis:7-alpine` | — (внутренний) | — | — | default | — (persistence отключён) |
| `bot` | build: `bot.Dockerfile` | — (long polling) | migrations: `service_completed_successfully`; nlp, cobalt, redis: `service_started` | — | default + dokploy-network | — |
| `nlp` | build: `nlp/Dockerfile`, context `.` | `8001:8000` | — | python urllib → `http://localhost:8000/health`, interval 30s, timeout 5s, retries 3, **start_period 90s** (модели грузятся долго) | default | `hf_cache:/app/.cache/huggingface` |
| `api` | build: `api/Dockerfile`, context `.` | `8002:8000` | migrations: `service_completed_successfully`; redis: `service_started` | — | default + dokploy-network | — |
| `miniapp` | build: `miniapp/Dockerfile`, build-arg `VITE_API_BASE_URL: ${MINIAPP_API_BASE_URL:-/api/v1}` | `8003:80` | — | — | default | — |
| `cobalt` | build: `./cobalt` (форк-образ) | — (внутренний, порт 9000 наружу НЕ публикуется) | — | — | default | — |
| ~~`postgres`~~ | `postgres:14` — **ЗАКОММЕНТИРОВАН**: используется managed-БД Dokploy | ~~5432:5432~~ | | | | ~~pgdata~~ |

VPN-сервисов нет. **Исключаемое для Yuvi v2**: закомментированный `API_EXTERNAL_PROXY: "http://user:pass@host:port"` у cobalt (прокси для обхода датацентр-блока; в комментарии предупреждение: «ПУСТУЮ строку не задавать — cobalt ломает tunnel (500)»).

### 2.2 Ключевые фрагменты compose (паттерны для заимствования)

Миграции как одноразовый сервис + гейт запуска бота:

```yaml
  migrations:
    build: { context: ., dockerfile: bot.Dockerfile }
    env_file: [./.env]
    command: alembic upgrade head
    networks: [default, dokploy-network]

  bot:
    build: { context: ., dockerfile: bot.Dockerfile }
    env_file: [./.env]
    environment:
      - NLP_SERVICE_URL=http://nlp:8000
      - REDIS_URL=${REDIS_URL:-redis://redis:6379/0}
    depends_on:
      migrations: { condition: service_completed_successfully }
      nlp:        { condition: service_started }
      cobalt:     { condition: service_started }
      redis:      { condition: service_started }
```

Redis как чистый кэш/pub-sub (без персистентности):

```yaml
  redis:
    image: redis:7-alpine
    restart: unless-stopped
    command: ["redis-server", "--save", "", "--appendonly", "no"]
```

Healthcheck NLP без curl в образе (через python stdlib):

```yaml
    healthcheck:
      test: ["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')"]
      interval: 30s
      timeout: 5s
      retries: 3
      start_period: 90s
```

Сети и volumes:

```yaml
networks:
  default:
  # Внешняя сеть Dokploy: в ней резолвится хост managed-БД
  # normaldata-tgbot-644xzi (PG18+pgvector). Создаётся Dokploy,
  # помечаем external — compose её не создаёт/не удаляет.
  dokploy-network:
    external: true

volumes:
  hf_cache:   # кэш HuggingFace-моделей nlp-сервиса
```

Обрати внимание: к `dokploy-network` подключены только сервисы, которым нужна БД (migrations, bot, api). nlp/miniapp/cobalt/redis живут в default.

---

## 3. Dockerfiles (все 5)

### 3.1 `bot.Dockerfile` (bot + migrations)

```dockerfile
FROM python:3.11
WORKDIR /app
COPY . .
RUN chmod +x scripts/wait-for-postgres.sh
RUN apt-get update && apt-get install -y netcat-openbsd && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir -r requirements.txt
ENV PYTHONPATH=/app
CMD ["python", "bot/main.py"]
```

- Полный (не slim) python:3.11; netcat нужен для `wait-for-postgres.sh` (сейчас entrypoint закомментирован в compose, ожидание заменено на managed-БД).
- Антипаттерн (не заимствовать): `COPY . .` ДО `pip install` — ломает кэш слоёв на каждом изменении кода.

### 3.2 `api/Dockerfile`

```dockerfile
FROM python:3.12-slim
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends build-essential && rm -rf /var/lib/apt/lists/*
COPY api/requirements.txt /app/api_requirements.txt
RUN pip install --no-cache-dir -r /app/api_requirements.txt
COPY . /app
ENV PYTHONPATH=/app:/app/bot
EXPOSE 8000
CMD ["sh", "-c", "uvicorn api.main:app --host 0.0.0.0 --port 8000 --workers ${API_WORKERS:-3}"]
```

- Ключевой трюк: `PYTHONPATH=/app:/app/bot` — API-сервис **переиспользует bot/services/** (economy_service, market_service и т.д.) без выделения общей библиотеки. Число uvicorn-воркеров — env `API_WORKERS` (default 3).

### 3.3 `nlp/Dockerfile`

```dockerfile
FROM python:3.11-slim
...
RUN pip install --no-cache-dir --index-url https://download.pytorch.org/whl/cpu torch \
    && pip install --no-cache-dir -r /app/requirements.txt
COPY nlp /app/nlp
ENV PYTHONPATH=/app
ENV HF_HOME=/app/.cache/huggingface
EXPOSE 8000
CMD ["uvicorn", "nlp.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

- **CPU-версия torch** через `--index-url https://download.pytorch.org/whl/cpu` (сильно меньше образ). `HF_HOME` указывает в volume `hf_cache` — модели не перекачиваются при пересоздании контейнера.

### 3.4 `miniapp/Dockerfile` (multi-stage: SvelteKit → nginx)

```dockerfile
FROM node:22-alpine AS build
WORKDIR /app
COPY miniapp/package.json miniapp/package-lock.json* ./
RUN npm install --no-audit --no-fund
COPY miniapp ./
ARG VITE_API_BASE_URL=/api/v1
ENV VITE_API_BASE_URL=$VITE_API_BASE_URL
RUN npm run build

FROM nginx:alpine
COPY miniapp/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/build /usr/share/nginx/html
EXPOSE 80
```

- `VITE_API_BASE_URL` вшивается на build-time (через build-arg из compose: `${MINIAPP_API_BASE_URL:-/api/v1}`). По умолчанию относительный `/api/v1` — то есть API и miniapp предполагаются за одним reverse-proxy доменом.
- Стек miniapp: `@sveltejs/kit ^2.5`, `svelte ^4.2.9`, `@sveltejs/adapter-static ^3` (SPA: `fallback: 'index.html'`, `prerender: { entries: [] }`), `vite ^5.1`, `typescript ^5.3`, `lightweight-charts ^4.2` (графики рынков).

### 3.5 `cobalt/Dockerfile` (форк cobalt для скачивания медиа)

```dockerfile
# Запинено по digest = ровно та версия (cobalt 11.7.1), из которой
# извлечены и пропатчены файлы в patched/.
FROM ghcr.io/imputnet/cobalt@sha256:63186dd68afd57ce3bb1f62cc4c139f5fa95b9c3e87a3cf5c6e4c7a570523f62
COPY patched/instagram.js    /app/src/processing/services/instagram.js
COPY patched/match-action.js /app/src/processing/match-action.js
COPY patched/request.js      /app/src/processing/request.js
```

- Патч добавляет поле `caption` (текст поста Instagram/TikTok) в JSON-ответ cobalt. В шапке Dockerfile — процедура апгрейда: `docker pull ghcr.io/imputnet/cobalt:latest` → извлечь свежие 3 файла из образа → перенести патч (искать `caption`) → обновить digest.
- В compose обязательна env `API_URL: "http://cobalt:9000/"` (self-reference cobalt-а). Порт наружу не публикуется, бот ходит по внутренней сети (`COBALT_API_URL=http://cobalt:9000/` в `bot/services/media_dl_service.py`).

---

## 4. Конфигурация: env-переменные

**Файла `.env.example` в репозитории НЕТ** (в `.gitignore` есть исключение `!.env.example`, но файл не закоммичен). **pydantic-settings НЕ используется** — вся конфигурация через `os.getenv(...)` с inline-дефолтами, разбросанными по сервисам, плюс `python-dotenv`: `common/db/db.py` делает `load_dotenv(PROJECT_ROOT / '.env')`. Каталог `config/` содержит ТОЛЬКО промпты (`config/prompts/*.md`), загружаемые через `common/prompts.py`:

```python
PROMPTS_DIR = Path(__file__).resolve().parent.parent / "config" / "prompts"

@lru_cache(maxsize=64)
def load(name: str) -> str:
    return (PROMPTS_DIR / f"{name}.md").read_text(encoding="utf-8").strip()

def render(name: str, **variables) -> str:
    text = load(name)
    return text.format(**variables) if variables else text
```

Промпты: `ask_system/task`, `ask_query_rewrite_system/task`, `digest_system/task`, `joke_system/task`, `phrase_system/task`, `summary_system/task`, `user_card_system/task`.

Полезный микро-паттерн `_env()` из `bot/services/ai_client.py` (fallback-имена переменных, использовался при миграции с OpenRouter):

```python
def _env(*names: str, default: str = "") -> str:
    for n in names:
        v = os.getenv(n)
        if v is not None and v != "":
            return v
    return default
# пример: MAX_MESSAGES = int(_env("AI_MAX_MESSAGES", "OPENROUTER_MAX_MESSAGES", default="1000"))
```

### 4.1 Полная опись env-переменных (имя → дефолт → назначение → где)

**Ядро / секреты:**

| Переменная | Дефолт | Назначение |
|---|---|---|
| `TELEGRAM_TOKEN` | — (обязателен) | токен бота; используется в `bot/main.py` и в `api/auth.py` (HMAC initData + getChatMember) |
| `DATABASE_URL` | — (обязателен) | `postgresql://user:pass@host:5432/db`; `common/db/db.py` падает с подсказкой, если нет |
| `POSTGRES_USER/PASSWORD/HOST/PORT/DB` | — | fallback для alembic: `migrations/env.py` собирает URL из них, если `DATABASE_URL` не задан |
| `REDIS_URL` | `""` (в compose `redis://redis:6379/0`) | pub/sub балансов (SSE) + перф-метрики; **best-effort: пусто → фичи тихо отключаются** |
| `REDIS_HOST`, `REDIS_PORT` | — | только в заготовке `worker/main.py` (RQ, не используется) |
| `OPENCODE_API_KEY` | — (обязателен для AI) | ключ OpenCode Zen Go |
| `OPENCODE_BASE_URL` | `https://opencode.ai/zen/go/v1` | OpenAI-compatible endpoint |
| `BOT_ADMIN_IDS` | `""` | список Telegram ID админов через запятую; проверка в `api/auth.py:is_admin()` и админ-хендлерах |
| `LOG_LEVEL` | `INFO` | уровень логирования (`common/logger/logger.py`) |
| `BUILD_SHA`, `BUILD_TIME` | `—` | инфо о сборке для `/admin_status` (подставлять при деплое) |
| `API_WORKERS` | `3` | число uvicorn-воркеров api (CMD Dockerfile) |
| `API_CORS_ORIGINS` | `*` | CORS для api (список через запятую) |

**Mini App auth:**

| Переменная | Дефолт | Назначение |
|---|---|---|
| `TMA_INIT_DATA_MAX_AGE` | `86400` | макс. возраст initData (сек) |
| `TMA_MEMBERSHIP_CACHE_TTL` | `300` | TTL кэша getChatMember |
| `MINIAPP_DEEPLINK` | — | переопределить deep-link `/casino` (иначе `https://t.me/<bot_username>?startapp=<chat_id>`) |
| `MINIAPP_API_BASE_URL` | `/api/v1` | build-arg miniapp (compose) |

**AI-лимиты (`bot/services/ai_client.py`, `summary_service.py`):**

| Переменная | Дефолт |
|---|---|
| `SUMMARY_MODEL` | `opencode-go/qwen3.5-plus` |
| `AI_AVAILABLE_MODELS` | — (список для `/model_list` через запятую) |
| `AI_MAX_MESSAGES` | `1000` |
| `AI_MAX_INPUT_TOKENS` | `12000` |
| `AI_MAX_OUTPUT_TOKENS` | `16000` |
| `AI_MAX_CHARS_PER_MESSAGE` | `800` |
| `AI_MAX_CUSTOM_PROMPT_CHARS` | `1200` |
| `AI_CALL_TIMEOUT_SEC` / `AI_STREAM_TIMEOUT_SEC` | `300` / `300` |

**NLP-сервис и воркеры классификации/эмбеддингов:**

| Переменная | Дефолт |
|---|---|
| `NLP_SERVICE_URL` | `http://nlp:8000` |
| `NLP_SENTIMENT_MODEL` | `seara/rubert-tiny2-russian-sentiment` |
| `NLP_TOXICITY_MODEL` | `cointegrated/rubert-tiny-toxicity` |
| `NLP_EMBED_MODEL` | `sentence-transformers/paraphrase-multilingual-mpnet-base-v2` (768-dim) |
| `NLP_MAX_LENGTH` / `NLP_EMBED_MAX_LENGTH` | `256` / `512` |
| `NLP_BATCH_SIZE` / `NLP_EMBED_BATCH_SIZE` | `32` / `64` |
| `NLP_WORKER_BATCH` | `200` (сообщений за проход classify) |
| `NLP_HTTP_TIMEOUT_SEC` | `60` |
| `NLP_EMBED_TIMEOUT_SEC` | `60` (worker) / `120` (backfill) |
| `EMBED_WORKER_BATCH` | `100` |
| `EMBED_MIN_TEXT_LEN` | `10` (короче — не эмбеддим) |

**RAG-поиск `/ask`:** `ASK_TOP_K=25`, `ASK_PER_QUERY_K=15`, `ASK_REWRITE_VARIANTS=3`, `ASK_NEIGHBORS_EACH_SIDE=2`.

**Экономика (гривны):** `ECONOMY_START_BONUS=1000`, `TRANSFER_FEE_PCT=5`, `TRANSFER_FEE_MIN=1`.

**Рынки ставок:** `MARKET_CREATION_FEE=100`, `MARKET_IMPORT_FEE=50`, `MARKET_MIN_BET=10`, `MARKET_RESOLUTION_FEE_PCT=5`, `MARKET_ANCHOR_RATE=100`, `MARKET_R_H0=200000`, `MARKET_TAU_MIN=240`, `MARKET_TICK_MIN=10`, `MARKET_PRICE_RETAIN_DAYS=7`, `EXTERNAL_MARKETS_HTTP_TIMEOUT=30`.

**Казино/дуэли:** `CASINO_MIN_BET=10`, `CASINO_MAX_BET=100000`; `DUEL_MIN_STAKE=10`, `DUEL_MAX_STAKE=100000`, `DUEL_FEE_PCT=5`.

**Ферма-кликер (все с префиксом `CLICKER_`):** `TAP_UPGRADE_BASE=50`, `AUTO_UPGRADE_BASE=200`, `UPGRADE_GROWTH=1.15`, `AUTO_RATE=0.5`, `MAX_TAP_LEVEL=50`, `MAX_AUTO_LEVEL=100`, `MAX_WORKER_LEVEL=50`, `MAX_CPS=30`, `OFFLINE_CAP_HOURS=4`, `WORKER_TIER_T2=10`, `WORKER_TIER_T3=25`; воркеры: `W_CHERRY_COST=50/RATE=0.2`, `W_LEMON_COST=250/RATE=0.5`, `W_BELL_COST=1200/RATE=1.5`, `W_STAR_COST=6000/RATE=5`, `W_DIAMOND_COST=30000/RATE=20`.

**Гача:** `GACHA_ROLL_COST=300`, `GACHA_X10_COST` (default `ROLL_COST*9`), `GACHA_SSR_PITY=50`, `GACHA_UR_PITY=90`, `GACHA_BANNER_RATEUP=0.5`.

**Номинации/теги/соцфичи:** `NOMINATION_PRIZE=300`, `NOMINATION_FAG=500`, `NOMINATION_MIN_MESSAGES=5`, `NOMINATION_MIN_QUOTE_CHARS=30`, `NOMINATION_MIN_QUOTE_REACTIONS=2`, `NOMINATION_ACTIVE_WINDOW_DAYS=14`; `TAG_RENT_PER_DAY=500`; `SOCIAL_POKE_COST=50`, `SOCIAL_JOKE_COST=150`, `SOCIAL_ROAST_COST=300`.

**Медиа-скачивание (cobalt):** `COBALT_API_URL=http://cobalt:9000/`, `MEDIADL_COST=50` (гривны), `MEDIADL_MAX_MB=48` (лимит Bot API 50 МБ), `MEDIADL_CAPTION_MAX=600`.

**Обратная связь:** `FEEDBACK_REWARD_BUG=500`, `FEEDBACK_REWARD_IDEA=300`.

**Импорт истории (`scripts/history_load.py`, pyrogram/userbot):** `TG_API_ID`, `TG_API_HASH`, `TG_PHONE`, `TG_SESSION_NAME` (получаются на my.telegram.org).

**DB-пул:** `DB_POOL_SIZE=20`, `DB_MAX_OVERFLOW=30`, `DB_POOL_TIMEOUT=10`.

### 4.2 Подключение к БД (`common/db/db.py`) — паттерн

```python
engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,
    pool_recycle=1800,
    pool_size=int(os.getenv("DB_POOL_SIZE", "20")),
    max_overflow=int(os.getenv("DB_MAX_OVERFLOW", "30")),
    pool_timeout=int(os.getenv("DB_POOL_TIMEOUT", "10")),
    connect_args={"keepalives": 1, "keepalives_idle": 30,
                  "keepalives_interval": 10, "keepalives_count": 5},
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
```

Синхронный SQLAlchemy (psycopg2) + `asyncio.to_thread(...)` в async-коде. TCP keepalive критичен для managed-БД за прокси Dokploy.

---

## 5. Логирование

**structlog НЕ используется** (несмотря на ожидания — эталон проще). Реализация — `common/logger/logger.py`:

- `get_logger(name)` — обычный `logging.getLogger` с защитой от повторного добавления хендлеров (`if logger.handlers: return logger`).
- Два хендлера: `StreamHandler` (stdout, для docker logs) + `RotatingFileHandler` в `logs/YYYY-MM-DD.log` (maxBytes=10 МБ, backupCount=5). Каталог `logs/` создаётся при импорте.
- Формат: `%(asctime)s - %(name)s - %(levelname)s - %(message)s`, уровень из `LOG_LEVEL` (default INFO).
- api и nlp используют голый `logging.basicConfig(level=logging.INFO, format=...)` в своих `main.py`.

Для Yuvi v2: можно взять паттерн `get_logger`, но structlog придётся добавлять самим — здесь его нет.

---

## 6. APScheduler — все регулярные задачи

Планировщик живёт **внутри процесса бота**: `bot/main.py` → `start_scheduler(bot)` из `bot/services/scheduler.py`. `AsyncIOScheduler(timezone=ZoneInfo("Europe/Moscow"))`. Каждый job — `coalesce=True, max_instances=1`. Module-level ссылка `_scheduler` + `get_scheduler()` — чтобы `/admin_status` показывал jobs и next_run_time.

| id job | Триггер | Функция | Что делает |
|---|---|---|---|
| `weekly_digest` | Cron: пн 09:00 МСК | `_weekly_digest_job(bot)` | ищет активные чаты (`find_active_chat_ids(window_days=14)`), для каждого с данными за 7 дней генерирует LLM-дайджест и шлёт чанками по 3900 символов (`_split_chunks`, разрыв по `\n`) |
| `nlp_classify_pending` | Interval: 30 сек | `classify_pending_once` | батч 200 (`NLP_WORKER_BATCH`) неклассифицированных сообщений → nlp `/classify/batch` → `messages.sentiment_label/sentiment_score/toxicity_score` |
| `embed_pending` | Interval: 45 сек | `embed_pending_once` | батч 100 (`EMBED_WORKER_BATCH`) сообщений длиной ≥ `EMBED_MIN_TEXT_LEN` → nlp `/embed/batch` → таблица `message_embeddings` (pgvector, 768) |
| `daily_nominations` | Cron: ежедневно 10:00 МСК | `run_daily_nominations(bot)` | «номинации дня» (в т.ч. «двойник/пидор дня» — `NOMINATION_FAG`), призы в гривнах |
| `external_markets_check` | Interval: 30 мин | `auto_resolve_external` | авторезолв импортированных внешних рынков |
| `markets_auto_close` | Interval: 5 мин | `auto_close_expired` | автозакрытие просроченных пользовательских рынков ставок |
| `tag_rentals_expire` | Interval: 5 мин | `_expire_tag_rentals_job` → `expire_due_sync` через `asyncio.to_thread` | истечение аренды тегов |
| `market_recover_tick` | Interval: `MARKET_TICK_MIN` мин (default 10, `max(1, int(...))`) | `recover_and_snapshot_all` | тик «биржевых» цен + снапшоты для графиков |
| `nomtag_expire` | Cron: ежедневно 00:05 МСК | `expire_nomination_tags(bot)` | снятие временных тегов-номинаций |

Паттерн регистрации:

```python
scheduler.add_job(
    _weekly_digest_job,
    trigger=CronTrigger(day_of_week="mon", hour=9, minute=0, timezone=MOSCOW_TZ),
    args=(bot,), id="weekly_digest", coalesce=True, max_instances=1,
)
```

Завершение: в `bot/main.py` `finally: scheduler.shutdown(wait=False)` после `dp.start_polling(bot)`.

---

## 7. Alembic и миграции

- `alembic.ini`: `script_location = %(here)s/migrations`, стандартный шаблон Alembic 1.17.
- `migrations/env.py`: импортирует ВСЕ модели вручную (User, Message, Reaction, BotSetting, DailyPick, MessageEmbedding, UserBalance, EconomyTx, ChatBank, Market/MarketOption/Bet), URL берёт так:

```python
DATABASE_URL = os.getenv(
    "DATABASE_URL",
    f"postgresql://{os.getenv('POSTGRES_USER')}:{os.getenv('POSTGRES_PASSWORD')}@{os.getenv('POSTGRES_HOST')}:{os.getenv('POSTGRES_PORT')}/{os.getenv('POSTGRES_DB')}"
)
config.set_main_option("sqlalchemy.url", DATABASE_URL)
```

- 16 ревизий в `migrations/versions/` с датированными именами: `20260513_01_nlp_fields`, `20260513_02_pgvector`, `20260514_01_embedding_768`, `20260514_02_economy`, `20260514_03_markets`, `20260514_04_casino`, `20260514_05_clicker`, `20260515_01_clicker_workers`, `20260515_02_duels`, `20260515_03_tag_rentals`, `20260515_04_gacha`, `20260519_01_feedback`, `20260519_02_feedback_reward`, `20260519_03_clicker_market`, `20260519_04_app_events`, `20260519_05_casino_idem`.
- Запуск в проде: сервис `migrations` (`command: alembic upgrade head`) — bot/api стартуют только после `service_completed_successfully`.
- Дублирующие идемпотентные скрипты `scripts/apply_*_migration.py` (pgvector/economy/markets/nlp/embedding_768) — ручное применение без Alembic (например `CREATE EXTENSION vector` требует superuser). `scripts/fix_alembic_state.py` — принудительно ставит `alembic_version` на head (лечение осиротевших ревизий): `docker compose run --rm --no-deps bot python scripts/fix_alembic_state.py`.

---

## 8. scripts/ — эксплуатация

### 8.1 БД: бэкапы и перенос (все через docker-образ `postgres:16`, чтобы зафиксировать версию pg_dump)

- **`db_backup.sh`** — регулярный бэкап по cron НА ХОСТЕ Dokploy: `0 */6 * * * DATABASE_URL=... /path/scripts/db_backup.sh`. `pg_dump -Fc --no-owner --no-privileges` → `backups/auto_<ts>.dump`, ротация: хранит `BACKUP_KEEP=28` последних.
- **`db_create.sh`** — создаёт ВЫДЕЛЕННУЮ БД бота на общем инстансе (`ADMIN_DATABASE_URL=.../postgres BOT_DB_NAME=xyloz_bot`), с regex-валидацией имени против DDL-инъекции.
- **`db_dump.sh` / `db_restore.sh`** — дамп/рестор (`pg_restore --no-owner --no-privileges --exit-on-error -j 4`, сознательно БЕЗ `--clean`).
- **`db_verify.sh`** — контрольная сводка для сверки источник↔цель: `alembic_version`, count по users/user_balance/chat_bank/economy_tx/messages/clicker_farms/gacha_collection/markets/bets/feedback + `sum(balance)` (деньги!).
- **`db_migrate.sh`** — оркестратор переноса с полным RUNBOOK в шапке: репетиция без даунтайма → CUTOVER (стоп bot+api → свежий дамп → restore → verify diff → флип `DATABASE_URL` в Dokploy env → редеплой → старую БД держать неделю для отката).

### 8.2 Прочее

- **`wait-for-postgres.sh`** — классический `until nc -z "$host" 5432; do sleep 1; done; exec "$@"` (сейчас не задействован).
- **`history_load.py`** — импорт истории чата userbot-ом (pyrogram + tgcrypto + tqdm), конфиг через `TG_API_ID/TG_API_HASH/TG_PHONE/TG_SESSION_NAME`, батч-коммиты по 100.
- **`nlp_backfill.py` / `embed_backfill.py` / `embed_backfill_local.py`** — догон классификации/эмбеддингов по историческим сообщениям: `docker compose exec bot python scripts/nlp_backfill.py [--max-batches N]`; `_local` — быстрый прогон на маке (MPS) в обход nlp-контейнера.
- **`manage_pgvector_index.py`** — `status|drop|create [--lists N]` для ivfflat-индекса на `message_embeddings`; рекомендация: DROP перед bulk-load, CREATE после (lists ≈ sqrt по числу строк).
- **`check_embeddings_coverage.py`, `debug_ask_recall.py`** — диагностика RAG.
- **`generate_miniapp_cover.py`** — PIL-генерация обложки **640×360** (`miniapp/casino-cover.png`) для регистрации Mini App у BotFather.

---

## 9. Деплой: Dokploy, nginx, HTTPS

Отдельного deploy-дока нет; картина восстанавливается из комментариев в коде:

1. **Платформа — Dokploy** (self-hosted PaaS поверх Docker + Traefik). Проект деплоится как compose-стек; env задаются в Dokploy UI (`.env` через `env_file`).
2. **PostgreSQL — managed-инстанс Dokploy** (хост `normaldata-tgbot-644xzi`, **PG18 + pgvector**), доступен через внешнюю docker-сеть `dokploy-network` (`external: true`). Секция `postgres:` в compose закомментирована. Ранее БД жила на внешнем хосте `2.59.43.142` — миграция описана в `db_migrate.sh`.
3. **HTTPS для Mini App** — в репозитории НЕТ конфигов TLS: nginx внутри miniapp-контейнера слушает голый :80, наружу проброшен `8003:80`. Термиция HTTPS — на уровне Dokploy/Traefik (Telegram Mini App требует валидный https-домен). API аналогично — `8002:8000`. Т.к. `VITE_API_BASE_URL=/api/v1` по умолчанию относительный, ожидается один публичный домен, где reverse-proxy маршрутизирует `/` → miniapp, `/api/v1` → api.
4. **nginx.conf miniapp** (`miniapp/nginx.conf`): SPA-fallback `try_files $uri $uri/ /index.html;` кэш статики 7d immutable; `index.html` — `no-store`; брендированная заглушка `error_page 500 502 503 504 /maintenance.html` (файл в `miniapp/static/`).
5. **Порты хоста:** nlp `8001`, api `8002`, miniapp `8003`; redis и cobalt наружу не публикуются; bot — long polling (входящих портов нет, webhook не используется).
6. **Бэкапы** — cron на хосте (`db_backup.sh`, каждые 6 часов) как «второй пояс» к бэкапам managed-БД.

---

## 10. Настройка BotFather / Telegram

- **Команды бота** регистрируются кодом в `bot/main.py::setup_commands()`: 18 публичных (`BotCommandScopeDefault`) + 10 админских поверх (`BotCommandScopeAllChatAdministrators`): help, summary, digest, card, mood, toxic, topics, ask, mystats, chatstats, who, peakday, streak, fag, joke, phrase, casino, rules; админ: model_show/list/set, prompt_show/set/reset, admin_status, backfill, fb, farmwipe.
- **Mini App**: регистрируется у BotFather как Main Mini App; обложка 640×360 генерится `scripts/generate_miniapp_cover.py`. Открытие из группового чата — **deep-link, а не WebAppInfo-кнопка** (`bot/handlers/casino.py`): «в групповых чатах WebAppInfo на inline-кнопке не работает (BUTTON_TYPE_INVALID)», поэтому:

```python
return f"https://t.me/{me.username}?startapp={chat_id}"
# chat_id прилетает в Mini App через initData.start_param
```

- **Аутентификация Mini App** (`api/auth.py`): валидация `X-Telegram-Init-Data` по официальной схеме HMAC (`secret = HMAC_SHA256(key=b"WebAppData", msg=bot_token)`, data_check_string из отсортированных пар, `hmac.compare_digest`), проверка `auth_date` (≤ `TMA_INIT_DATA_MAX_AGE`), затем **проверка членства в чате** прямым HTTP `getChatMember` с кэшем 5 мин (`TMA_MEMBERSHIP_CACHE_TTL`) и `asyncio.Lock` от stampede. Админство — по `BOT_ADMIN_IDS`.

---

## 11. Realtime и наблюдаемость

- **SSE-пуш баланса**: `common/events.py` — Redis pub/sub канал `bal:{chat_id}`, `publish_balance()` вызывается ПОСЛЕ commit, никогда не бросает (best-effort). `api/routes/events.py` — `GET /api/v1/events` через `StreamingResponse`; initData передаётся query-параметром (EventSource не умеет заголовки); заголовки `Cache-Control: no-cache`, `X-Accel-Buffering: no`; без Redis — деградация до heartbeat `: ping` каждые 20 с. Несколько uvicorn-воркеров ОК — Redis фанаутит.
- **Перф-метрики** (`common/metrics.py`): латентность каждого API-запроса в Redis по фикс-бакетам `[50,100,200,500,1000,2000,5000]` мс → p50/p95 без Prometheus; пул SQLAlchemy пишется не чаще раза в 2 с на воркер (middleware в `api/main.py`).
- **`/admin_status`** (`bot/services/admin_status_service.py`): health-пинги Postgres (`SELECT 1`), nlp (`/health`), OpenCode (`GET {base}/models` с Bearer); аптайм, `BUILD_SHA/BUILD_TIME`; покрытие NLP/эмбеддингов по чатам; топ-8 таблиц по `pg_total_relation_size`; список APScheduler-jobs с next_run.
- **Health-эндпоинты**: api — `/health`, `/api/v1/ping` («сервис ожил» для Mini App после редеплоя); nlp — `/health` (models_loaded).

---

## 12. Зависимости (requirements)

- **Корневой `requirements.txt`** (bot+migrations): `aiogram>=3.27,<4`, `SQLAlchemy`, `psycopg2-binary`, `emoji`, `python-dotenv`, `alembic==1.17.2`, `pyrogram`, `tgcrypto`, `tqdm`, `APScheduler>=3.10`, `tzdata`, `aiohttp>=3.9`, `openai>=1.40`, `numpy>=1.24`, `scikit-learn>=1.4`, `pgvector>=0.3`, `redis>=5`.
- **`api/requirements.txt`**: `fastapi>=0.110`, `uvicorn[standard]>=0.27`, `pydantic>=2.5`, `SQLAlchemy>=2`, `psycopg2-binary`, `python-dotenv`, `pgvector>=0.3`, `aiohttp>=3.9`, `openai>=1.40`, `redis>=5`.
- **`nlp/requirements.txt`**: `fastapi>=0.110`, `uvicorn[standard]>=0.27`, `pydantic>=2.5`, `transformers>=4.40`, `torch>=2.2` (CPU-wheel), `sentencepiece`, `protobuf`, `sentence-transformers>=2.7`.
- `worker/requirements.txt` — отсутствует/пустой (worker — мёртвая заготовка RQ: `Redis(host=REDIS_HOST) + Queue("messages")`, в compose не включён).

---

## 13. Что взять в Yuvi Bot v2 и что сделать иначе

**Заимствовать:**
1. Сервис `migrations` + `depends_on: service_completed_successfully` — гонок схемы нет.
2. Переиспользование bot/services в api через `PYTHONPATH=/app:/app/bot` (или лучше — честный общий пакет).
3. Multi-stage miniapp (node build → nginx) c build-arg `VITE_API_BASE_URL` и SPA-fallback + maintenance.html.
4. Форк cobalt оверлеем поверх digest-pinned образа (патч `caption`), внутренняя сеть без публикации порта.
5. Все паттерны APScheduler: MSK-таймзона, `coalesce/max_instances=1`, module-level `get_scheduler()` для админ-статуса.
6. Best-effort Redis (events/metrics не валят транзакции), SSE с деградацией до heartbeat.
7. api/auth.py целиком: HMAC initData + membership-кэш + `BOT_ADMIN_IDS`.
8. Комплект db_*.sh с runbook-ом cutover и сверкой `sum(balance)`.
9. Deep-link `?startapp=<chat_id>` вместо WebAppInfo в группах.
10. Healthcheck nlp с `start_period: 90s` и volume `hf_cache`.

**Сделать иначе (слабые места эталона):**
1. Ввести **pydantic-settings** (в эталоне его нет — 100+ разрозненных `os.getenv`); собрать единый `config/settings.py` и сгенерировать `.env.example` (в эталоне отсутствует).
2. **structlog** вместо самодельного логгера (в эталоне обычный logging + RotatingFileHandler).
3. В bot.Dockerfile: сначала `COPY requirements.txt` → `pip install` → потом `COPY . .` (кэш слоёв); slim-образ.
4. Выровнять версии Python (сейчас bot 3.11-full, api 3.12-slim, nlp 3.11-slim).
5. Удалить мёртвый `worker/` (RQ-заготовка) либо реализовать по-настоящему.
6. Не тащить прокси-настройки cobalt (`API_EXTERNAL_PROXY`) и любые VPN-механики — исключено по требованиям Yuvi v2.
7. Рассмотреть healthchecks для bot/api/redis (в эталоне healthcheck только у nlp).
