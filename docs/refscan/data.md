# Отчёт: слой данных эталона xyloz_tg_bot (модели БД, миграции, Redis/RQ, worker)

Источник: репозиторий `Heide172/xyloz_tg_bot` (клон: `...\scratchpad\xyloz_tg_bot`).
Зона анализа: `common/models/`, `common/db/`, `migrations/`, `worker/`, использование Redis, фоновые задачи, хранение сообщений/реакций/эмбеддингов.

---

## 1. Общая картина слоя данных

- **СУБД**: PostgreSQL (в проде — managed PG18 c расширением **pgvector**, подключение через внешнюю docker-сеть `dokploy-network`). Локальный сервис postgres в `docker-compose.yml` закомментирован.
- **ORM**: SQLAlchemy (классический declarative, `common/db/base.py`: `Base = declarative_base()`), **синхронные** сессии.
- **Подключение** (`common/db/db.py`): `create_engine(DATABASE_URL, pool_pre_ping=True, pool_recycle=1800, pool_size=DB_POOL_SIZE(20), max_overflow=DB_MAX_OVERFLOW(30), pool_timeout=DB_POOL_TIMEOUT(10), connect_args={keepalives...})`; `SessionLocal = sessionmaker(autocommit=False, autoflush=False)`. `.env` ищется от корня проекта.
- **Миграции**: Alembic (`alembic.ini`, `migrations/`), запускаются отдельным one-shot контейнером `migrations` (command: `alembic upgrade head`), от которого зависят `bot` и `api` (`condition: service_completed_successfully`) — хороший паттерн: код не стартует, пока схема не приведена к head.
- **Redis**: redis:7-alpine **без персистентности** (`--save "" --appendonly no`). Используется НЕ как очередь, а как: (а) pub/sub для пуша баланса в Mini App через SSE, (б) кросс-воркерные perf-метрики API.
- **worker/ и RQ**: фактически **мёртвый заглушечный код**. Все фоновые задачи живут в контейнере `bot` на APScheduler.

Итого сервисы compose: `migrations`, `redis`, `bot` (aiogram 3 + APScheduler), `nlp` (FastAPI + HF-модели, CPU), `api` (FastAPI), `miniapp` (SvelteKit + nginx), `cobalt` (форк с патчем caption).

---

## 2. Все модели SQLAlchemy (20 таблиц)

Все модели — в `common/models/*.py`, по одному-два класса на файл. Везде `BIGINT` PK autoincrement (BIGSERIAL), `created_at TIMESTAMP DEFAULT now()` (naive UTC, `datetime.utcnow`). Мультичатовость решена колонкой `chat_id BIGINT` в каждой таблице (без FK на таблицу чатов — таблицы чатов нет вообще).

### 2.1 users (`user.py`)
| колонка | тип | примечание |
|---|---|---|
| id | BIGINT PK | внутренний id |
| tg_id | BIGINT UNIQUE | telegram id |
| username | String | |
| fullname | String | |

Связь: `messages = relationship("Message", back_populates="user")`. Важный паттерн: **внутренний PK отделён от tg_id** — все FK ссылаются на `users.id`, а `tg_id` только для Bot API.

### 2.2 messages (`message.py`) — центральная таблица (100% сообщений)
| колонка | тип | примечание |
|---|---|---|
| id | BIGINT PK autoincrement | |
| message_id | BIGINT NOT NULL | tg message id (легаси-дубль) |
| telegram_message_id | BIGINT NOT NULL | tg message id (для истории/backfill) |
| user_id | BIGINT FK users.id, nullable | null для анонимных/каналов |
| chat_id | BIGINT NOT NULL | |
| text | Text | text или caption |
| emojis | String(255) | конкатенация эмодзи, извлечённых из текста при сохранении (`emoji.is_emoji`) |
| sticker | String(255) | file_id стикера |
| media | String(255) | file_id фото (последний размер) |
| reply_to | BIGINT | tg id сообщения-родителя (reply-граф) |
| message_type | String(50) default 'text' | text/photo/video/... |
| file_id / file_unique_id / file_name | String(255) | медиа |
| mime_type | String(100), file_size BIGINT | медиа |
| caption | Text; has_media Boolean; is_forwarded Boolean; forward_from String(255) | форвард — id источника строкой |
| created_at | DateTime default utcnow; edited_at DateTime | |
| sentiment_score | Float | -1.0..1.0 (знак = label, модуль = confidence) |
| sentiment_label | String(20) | positive/neutral/negative |
| toxicity_score | Float | 0.0..1.0 = 1 - P(non-toxic) |
| topic_id | Integer | зарезервировано, воркером не заполняется |
| nlp_processed_at | DateTime | маркер «обработано NLP»; NULL = pending |

Индексы (`__table_args__`):
- `idx_chat_telegram_message (chat_id, telegram_message_id) UNIQUE` — дедупликация при импорте истории;
- `idx_chat_message (chat_id, message_id)`;
- `idx_user_chat (user_id, chat_id)`;
- `idx_created_at (created_at)`;
- `idx_nlp_unprocessed (nlp_processed_at)` — выборка pending для NLP;
- `idx_chat_sentiment (chat_id, sentiment_label)`.

### 2.3 reactions (`reaction.py`)
`id BIGINT PK; message_id FK messages.id; user_id FK users.id; emoji String; date DateTime default utcnow`. Связи: `message.reactions` / `reaction.message`, `reaction.user`. ВАЖНО: `message_id` пишется **сырой tg message_id события**, не внутренний `messages.id` (баг/упрощение эталона — join по `Reaction.message_id == Message.id` в digest/nominations из-за этого хрупкий; в новом проекте резолвить в внутренний id через `idx_chat_telegram_message`). Снятие реакции не удаляет строку — хранится только append-only лог `new_reaction`.

### 2.4 message_embeddings (`message_embedding.py`) — pgvector
```python
from pgvector.sqlalchemy import Vector
message_id = Column(BIGINT, ForeignKey("messages.id", ondelete="CASCADE"), primary_key=True)
chat_id    = Column(BIGINT, nullable=False, index=True)
embedding  = Column(Vector(768), nullable=False)
created_at = Column(DateTime, default=datetime.utcnow)
```
1:1 к messages (PK = FK). Модель эмбеддинга: `sentence-transformers/paraphrase-multilingual-mpnet-base-v2` (768-dim, normalize_embeddings=True). Векторный индекс: `ivfflat (embedding vector_cosine_ops)`, создаётся ПОСЛЕ backfill скриптом `scripts/manage_pgvector_index.py` (drop перед bulk-load, `create --lists sqrt(count)`, min 10).

### 2.5 bot_settings (`bot_setting.py`) — KV-настройки в БД
`key String(100) PK; value Text; updated_by_tg_id BIGINT; updated_at DateTime`. Используется для смены AI-модели и промптов на лету (`/model_set`, `/prompt_set`).

### 2.6 daily_picks (`daily_pick.py`) — «участник дня» (аналог «двойника дня»)
`id PK; chat_id; day_msk Date; winner_tg_id BIGINT; title String(64) (напр. "participant_of_day"); picked_by_tg_id; created_at`.
Уникальный индекс `idx_daily_pick_chat_day_title (chat_id, day_msk, title) UNIQUE` — идемпотентность «одного победителя в день на чат на номинацию». Хранит **tg_id**, не внутренний id.

### 2.7 user_balance (`user_balance.py`) — экономика «гривны»
Композитный PK `(user_id, chat_id)` (`PrimaryKeyConstraint`), `balance Integer NOT NULL default 0`, `created_at/updated_at`. FK `users.id ON DELETE CASCADE`. Индекс `idx_user_balance_chat (chat_id)`.

### 2.8 economy_tx (`economy_tx.py`) — append-only леджер
`id BIGSERIAL PK; user_id FK SET NULL; chat_id; amount Integer (плюс=пополнение, минус=списание); kind String(40); ref_id String(80); note Text; created_at (index)`.
Индексы: `(user_id, chat_id)`, `(chat_id, kind)`, `(created_at)`. Виды kind (из сервисов): `start_bonus`, ставки/выплаты рынков, казино, дуэли, переводы, аренда тегов, награды за фидбек, fag-бонус и т.д. `ref_id` — связь с сущностью (id игры/рынка/дуэли).

### 2.9 chat_bank (`chat_bank.py`)
`chat_id BIGINT PK; balance Integer; updated_at`. «Банк чата» — сюда идут комиссии (переводы 5% min 1, дуэли) и проигрыши.

### 2.10 markets / market_options / bets (`market.py`) — рынки ставок
- **markets**: `id PK; chat_id; type String(20) default 'internal' (internal|polymarket|manifold); question Text; creator_id FK SET NULL; status String(20) default 'open' (open|closed|resolved|cancelled); closes_at NOT NULL; resolved_at; winning_option_id BIGINT; external_url Text; external_id String(120); created_at`. Индексы `(chat_id, status)`, `(closes_at)`.
- **market_options**: `id PK; market_id FK CASCADE; label String(200); pool Integer default 0 (сумма ставок на опцию — parimutuel); position Integer`. Индекс `(market_id)`. Связь cascade="all, delete-orphan".
- **bets**: `id PK; market_id FK CASCADE; option_id FK CASCADE; user_id FK SET NULL; amount Integer; payout Integer NULL (заполняется при resolve); refunded Integer default 0 (1 = возврат при cancel); created_at`. Индексы `(market_id, option_id)`, `(user_id)`.

### 2.11 casino_games (`casino_game.py`)
`id PK; chat_id; user_id FK SET NULL; game String(20) (coinflip|dice|slots|blackjack|roulette); bet Integer; payout Integer default 0 (gross ≥0, net = payout - bet); status String(20) default 'finished' (active|finished|cancelled); outcome String(20) (win|lose|push|blackjack); state JSONB (для многоходового блэкджека); created_at; finished_at; idem_key String(40) NULL`.
Индексы: `(user_id, chat_id)`, `(status)`, `(game)` + **частичный уникальный** `uq_casino_games_user_idem ON (user_id, idem_key) WHERE idem_key IS NOT NULL` — идемпотентность: ретрай запроса с тем же ключом не списывает ставку повторно (защита от двойного списания при редеплое).

### 2.12 clicker_farms (`clicker_farm.py`) — ферма-кликер
`id PK; user_id FK CASCADE; chat_id; cp_balance BIGINT default 0 («click points»); tap_level Integer default 1 (доход за тап = tap_level); auto_level Integer default 0 (легаси автокликер); workers JSONB default {} (легаси: {"cherry": level,...}, мигрируется в гачу); lifetime_cp BIGINT; pity_ssr Integer; pity_ur Integer; gacha_rolls Integer; active_heroine String(40) (char_id); gacha_migrated Integer 0/1; last_seen_at DateTime (offline-доход: auto_level×rate×elapsed, кап OFFLINE_CAP_HOURS); created_at/updated_at`.
`UniqueConstraint(user_id, chat_id, name="uq_clicker_farms_user_chat")`, индекс `(chat_id)`. Pity-счётчики гачи хранятся прямо в ферме.

### 2.13 clicker_market_pool / clicker_market_price (`clicker_market.py`) — AMM-обменник cp↔гривна
- **clicker_market_pool**: `chat_id PK; r_cp Float; r_h Float; updated_at`. Constant-product AMM: курс = r_cp/r_h. Резервы float, восстановление к якорю — экспонента.
- **clicker_market_price**: `id PK; chat_id; ts; rate Float (cp за 1 гривну)` + индекс `(chat_id, ts)` — снапшоты курса для графика «живого рынка». Константы (`market_service.py`): `MARKET_ANCHOR_RATE=100`, `MARKET_R_H0=200000`, `MARKET_TAU_MIN=240`, `MARKET_TICK_MIN=10`, `MARKET_PRICE_RETAIN_DAYS=7`; тик: `factor = exp(-TICK_MIN/TAU_MIN)`, все пулы подтягиваются к якорю + пишется снапшот.

### 2.14 duels (`duel.py`)
`id PK; chat_id; challenger_id FK SET NULL; opponent_id FK SET NULL; stake Integer; status default 'pending' (pending|resolved|declined|cancelled); winner_id BIGINT; commission Integer default 0; created_at; resolved_at`.
Индексы `(chat_id, status)`, `(opponent_id, status)`, `(challenger_id, status)`. Эскроу: stake challenger списывается при вызове, opponent — при принятии; победитель получает 2×stake − комиссия (в банк чата).

### 2.15 tag_rentals (`tag_rental.py`) — аренда кастомного тега (custom_title админа)
`id PK; chat_id; user_id FK SET NULL; tg_user_id BIGINT NOT NULL (дубль для Bot API — чтобы снять титул даже если user удалён); title String(32); price_paid Integer; rented_at; expires_at NOT NULL; status String(16) default 'active' (active|expired|cancelled); created_at`.
Индексы `(chat_id, status)`, `(user_id, status)`, `(status, expires_at)` — последний под шедулер экспирации.

### 2.16 gacha_collection (`gacha_collection.py`)
`id PK; user_id FK CASCADE; chat_id; char_id String(40); stars Integer default 1 (дубликат повышает, макс 5); copies Integer default 1 (всего выпало, для возврата сверх 5★); obtained_at`.
`UNIQUE (user_id, chat_id, char_id)`, индекс `(user_id, chat_id)`. Сам каталог персонажей — НЕ в БД, а в коде (`bot/services/gacha_catalog.py`).

### 2.17 feedback (`feedback.py`)
`id PK; user_id FK SET NULL; chat_id NULL; kind String(16) (bug|idea); text Text; status String(16) default 'new' (new|seen|done); created_at; reward Integer default 0; rewarded_at`. Индексы `(kind, status)`, `(created_at)`.

### 2.18 app_events (`app_event.py`) — usage-аналитика Mini App
`id PK; user_id FK SET NULL; chat_id NULL; event String(48) (view|action|...); props JSONB default {} ({route|name|...}); ts`. Индексы `(ts)`, `(event, ts)`.

---

## 3. Alembic-миграции: порядок и содержание

`migrations/env.py`: URL из `DATABASE_URL` (fallback — сборка из `POSTGRES_USER/PASSWORD/HOST/PORT/DB`), явный импорт всех моделей, `target_metadata = Base.metadata`.

ВАЖНО: **базовые таблицы (users, messages, reactions, bot_settings, daily_picks) миграциями НЕ создаются** — исторически создавались через `Base.metadata.create_all` (`common/db/init_db.py`, `db.py:init_db()`). Первая миграция уже ALTER'ит messages. Все миграции написаны **сырым SQL через `op.execute` c `IF NOT EXISTS`** — идемпотентны и переживают дрейф состояния (есть даже `scripts/fix_alembic_state.py`). Линейная цепочка из 17 ревизий:

| # | revision | содержание |
|---|---|---|
| 1 | `20260513_01` nlp_fields | +5 NLP-колонок в messages + индексы idx_nlp_unprocessed, idx_chat_sentiment |
| 2 | `20260513_02` pgvector | `CREATE EXTENSION IF NOT EXISTS vector`; message_embeddings vector(384); ivfflat lists=100 |
| 3 | `20260514_01` embedding_768 | DROP+CREATE message_embeddings под vector(768) (mpnet); эмбеддинги уничтожаются, требуется backfill; ivfflat создаётся позже скриптом |
| 4 | `20260514_02` economy | user_balance, economy_tx, chat_bank + индексы |
| 5 | `20260514_03` markets | markets, market_options, bets + индексы |
| 6 | `20260514_04` casino | casino_games + 3 индекса |
| 7 | `20260514_05` clicker | clicker_farms (ещё с daily_converted, daily_window_start) |
| 8 | `20260515_01` clicker_workers | +workers JSONB default '{}' |
| 9 | `20260515_02` duels | duels + 3 индекса |
| 10 | `20260515_03` tag_rentals | tag_rentals + 3 индекса |
| 11 | `20260515_04` gacha | gacha_collection + 5 гача-колонок в clicker_farms (цикл ADD COLUMN IF NOT EXISTS) |
| 12 | `20260519_01` feedback | feedback + индексы |
| 13 | `20260519_02` feedback_reward | +reward, +rewarded_at |
| 14 | `20260519_03` clicker_market | clicker_market_pool, clicker_market_price; DROP daily_converted/daily_window_start (daily-cap заменён AMM) |
| 15 | `20260519_04` app_events | app_events + индексы |
| 16 | `20260519_05` casino_idem | +idem_key; partial unique index `(user_id, idem_key) WHERE idem_key IS NOT NULL` |

Дополнительно есть ручные скрипты применения отдельных миграций (`scripts/apply_*_migration.py`) и db-скрипты (`db_backup.sh`, `db_migrate.sh`, `wait-for-postgres.sh`).

---

## 4. Redis: что и как используется

Redis **не персистентный** и весь код к нему — best-effort (недоступен → функциональность тихо деградирует, основная транзакция не страдает). Env: `REDIS_URL` (default `redis://redis:6379/0` в compose), в worker-заглушке — `REDIS_HOST`/`REDIS_PORT`.

### 4.1 Pub/Sub для live-баланса в Mini App (`common/events.py` + `api/routes/events.py`)
- Канал: `bal:{chat_id}` (`balance_channel()`), payload `{"user_id": ..., "balance": ...}`.
- `publish_balance()` вызывается **ПОСЛЕ commit** из `economy_service.credit()/debit()` (обёртка `_publish` в try/except pass). Клиент с `socket_timeout=2`, ленивая инициализация singleton.
- SSE-эндпоинт `GET /events` (FastAPI, `StreamingResponse`, `text/event-stream`): auth через query-параметры (EventSource не умеет заголовки), `redis.asyncio` pubsub-подписка на канал чата, фильтрация по `user_id`, heartbeat `: ping` каждые 20с; без Redis — только heartbeat. Несколько uvicorn-воркеров ок: Redis фанаутит всем.

### 4.2 Перф-метрики API (`common/metrics.py`)
Кросс-воркерная статистика запросов без Prometheus:
- `perf:routes` — SET ключей маршрутов; на ключ `perf:r:{method}:{route}` — HASH: `n`, `sum` (мс), `err4`, `err5`, `max`, `b0..b7` — фикс-бакеты латентности `[50,100,200,500,1000,2000,5000,+inf]` → грубые p50/p95 без зависимостей.
- `perf:pool` — HASH `pid -> "checked_out/size+overflow"` по воркерам, `EXPIRE 120` (единственный TTL в проекте).
- `record_request()` пишется middleware в `api/main.py`; `snapshot(top=25)` читается командой `/admin_status` и админ-API; `reset()` — очистка.

### 4.3 Чего в Redis НЕТ
Кэша доменных данных с TTL нет; очередей нет; FSM-хранилища aiogram нет (polling + стейт в PG). README обещает «Redis: queues (RQ), кэш» — это аспирационная документация, код разошёлся.

---

## 5. worker/ и RQ: фактическое состояние

`worker/main.py` — 15 строк, заглушка:
```python
redis_conn = Redis(host=os.getenv("REDIS_HOST"), port=os.getenv("REDIS_PORT"))
queue = Queue("messages", connection=redis_conn)   # rq
def test_job(name): print(f"Processing job: {name}")
```
`worker/requirements.txt` пуст, Dockerfile отсутствует, сервиса в docker-compose нет, `rq` нет даже в корневом requirements.txt. Очередь `messages` нигде не консюмится. **Вывод для нового проекта: RQ-воркер в эталоне не реализован; всю фоновую работу выполняет APScheduler внутри процесса бота.** Если нужен честный worker-контейнер — проектировать заново (например ARQ/RQ + отдельный сервис), из эталона брать только состав задач (ниже).

---

## 6. Фоновые задачи (реальный «worker» = APScheduler в bot)

`bot/services/scheduler.py`, `AsyncIOScheduler(timezone=Europe/Moscow)`, стартует в `bot/main.py` перед polling, каждый job с `coalesce=True, max_instances=1`; ссылка на scheduler — module-level `_scheduler` для отображения jobs в `/admin_status`:

| job id | триггер | что делает |
|---|---|---|
| `weekly_digest` | Cron: Пн 09:00 МСК | `find_active_chat_ids(window_days=14)` → для чатов с данными за 7 дней `generate_digest()` → отправка чанками ≤3900 симв. |
| `nlp_classify_pending` | Interval 30s | `classify_pending_once()`: батч ≤200 сообщений `nlp_processed_at IS NULL` → POST `nlp:8000/classify/batch` → UPDATE sentiment/toxicity + nlp_processed_at |
| `embed_pending` | Interval 45s | `embed_pending_once()`: батч ≤100 сообщений len(text)≥10 без эмбеддинга (`NOT IN (SELECT message_id FROM message_embeddings)`, order by id DESC — свежие первыми) → POST `/embed/batch` → `session.merge(MessageEmbedding(...))` |
| `daily_nominations` | Cron 10:00 МСК | `run_daily_nominations(bot)` — ежедневные номинации (топ по реакциям и т.п.), выдача тегов |
| `external_markets_check` | Interval 30m | `auto_resolve_external()` — автопроверка резолюции Polymarket/Manifold рынков |
| `markets_auto_close` | Interval 5m | `auto_close_expired()` — закрытие рынков с истёкшим `closes_at` |
| `tag_rentals_expire` | Interval 5m | `expire_due_sync()` через `asyncio.to_thread` — снятие истёкших custom_title (индекс `(status, expires_at)`) |
| `market_recover_tick` | Interval `MARKET_TICK_MIN` (10m) | `recover_and_snapshot_all()` — экспоненциальный возврат AMM-пулов к якорю + снапшот цены в clicker_market_price |
| `nomtag_expire` | Cron 00:05 МСК | `expire_nomination_tags(bot)` — снятие суточных номинационных тегов |

Плюс **ручные backfill-джобы** (`bot/services/backfill_runner.py`, команда `/backfill`): реестр `JOB_REGISTRY = {"embed": embed_pending_once, "nlp": classify_pending_once}`, цикл `_run_loop` крутит батчи, останавливается после 3 холостых итераций (по 5с сна), считает processed/rate_per_sec/last_error — удобный паттерн прогресс-мониторинга долгих задач из чата.

---

## 7. Хранение сообщений и реакций

- **Live-приём**: catch-all хендлер `@router.message()` последним роутером (`bot/handlers/messages.py`) → `save_message(tg_message)` (`bot/services/message_service.py`): get-or-create User по tg_id → INSERT Message (text, emojis=конкатенация эмодзи из текста, sticker file_id, media=photo[-1].file_id, reply_to, created_at из tg_message.date). Каждый вызов — своя `SessionLocal()` c try/rollback/finally close.
- **Реакции**: `save_reaction(event: MessageReactionUpdated)` — get-or-create User, INSERT Reaction(emoji=event.new_reaction). (NB: хендлер в эталоне повешен на `@router.chat_member()` — по-видимому ошибка, должен быть `message_reaction`; в aiogram 3 нужен allowed_updates c `message_reaction`.)
- **Импорт истории**: `scripts/history_load.py` — Pyrogram-клиент (user-сессия), итерация по истории чата, дедупликация по `(chat_id, telegram_message_id)`, извлечение media_info/forward/reply_to/emojis, `text = message.text or message.caption`; заполняет оба поля message_id/telegram_message_id. После импорта NLP и эмбеддинги догоняются периодическими джобами или `/backfill`.

## 8. Частотные словари слов/эмодзи

**В БД никаких агрегатных таблиц частот нет** — всё считается на лету по сырым messages/reactions:
- Слова: `bot/handlers/statistic.py` — `_WORD_RE = re.compile(r"[A-Za-zА-Яа-яЁё][A-Za-zА-Яа-яЁё\-]{3,}")` + сет `_STOPWORDS`, `_top_words(texts, limit)` через `collections.Counter` (для /mystats, /chatstats, /who).
- «Характерные слова» дайджеста: `digest_service.py` — unigram/bigram `Counter` по окну vs фон (background) — контрастная частотность.
- Эмодзи сообщений: колонка `messages.emojis` (заполняется при сохранении), эмодзи реакций: `GROUP BY reactions.emoji ORDER BY count DESC` в `user_card_service.py` (топ полученных/поставленных реакций юзера).
Для нового проекта с большими объёмами: тот же подход + при необходимости materialized view / инкрементальные агрегаты, но эталон живёт на Counter'ах поверх выборок.

## 9. Эмбеддинги и /ask (RAG)

- Пайплайн записи: scheduler (45с) / backfill → `nlp:8000/embed/batch` (SentenceTransformer mpnet-base-v2, 768, normalize, max_seq 512, batch 64) → `message_embeddings`.
- `bot/services/ask_service.py`, `stream_ask()`:
  1) LLM переписывает вопрос в `ASK_REWRITE_VARIANTS=3` перефразировки (промпты `config/prompts/ask_query_rewrite_*.md`);
  2) эмбеддинг всех вариантов батчем;
  3) по каждому — векторный поиск `MessageEmbedding.embedding.cosine_distance(vec)` (pgvector), `ASK_PER_QUERY_K=15`, фильтр по chat_id;
  4) **гибрид**: лексический ILIKE-поиск по «корням» значимых слов (`_term_root`: срез окончания ≤2 букв) + опц. скоуп автора `@username` (similarity 0.97/0.80 + 0.01×matched);
  5) merge по max similarity → `ASK_TOP_K=25`;
  6) `_expand_with_neighbors`: ±`ASK_NEIGHBORS_EACH_SIDE=2` соседних сообщения вокруг каждого хита по created_at;
  7) контекст: хронологический список `★[sim=..] дата МСК @автор: текст(≤300)` → LLM-стриминг ответа (промпты `ask_system.md`/`ask_task.md`).
- Обслуживание: `scripts/embed_backfill.py` (через nlp-сервис), `embed_backfill_local.py` (локально на MPS), `check_embeddings_coverage.py`, `debug_ask_recall.py`, `manage_pgvector_index.py` (status/drop/create, lists=sqrt(rows)).

## 10. Ключевые env-переменные слоя данных

`DATABASE_URL`, `DB_POOL_SIZE=20`, `DB_MAX_OVERFLOW=30`, `DB_POOL_TIMEOUT=10`; `REDIS_URL`; `NLP_SERVICE_URL=http://nlp:8000`, `NLP_WORKER_BATCH=200`, `NLP_HTTP_TIMEOUT_SEC=60`, `EMBED_WORKER_BATCH=100`, `NLP_EMBED_TIMEOUT_SEC=120`, `EMBED_MIN_TEXT_LEN=10`, `NLP_SENTIMENT_MODEL=seara/rubert-tiny2-russian-sentiment`, `NLP_TOXICITY_MODEL=cointegrated/rubert-tiny-toxicity`, `NLP_EMBED_MODEL=...mpnet-base-v2`; `ASK_TOP_K=25`, `ASK_PER_QUERY_K=15`, `ASK_REWRITE_VARIANTS=3`, `ASK_NEIGHBORS_EACH_SIDE=2`; `ECONOMY_START_BONUS=1000`, `TRANSFER_FEE_PCT=5`, `TRANSFER_FEE_MIN=1`; `MARKET_ANCHOR_RATE=100`, `MARKET_R_H0=200000`, `MARKET_TAU_MIN=240`, `MARKET_TICK_MIN=10`, `MARKET_PRICE_RETAIN_DAYS=7`; `TELEGRAM_TOKEN`.

## 11. Паттерны, которые стоит заимствовать

1. **Леджер экономики**: mutable `user_balance` + append-only `economy_tx(kind, ref_id)`; `SELECT ... FOR UPDATE` (`with_for_update()`) на балансе и банке в одной транзакции; публикация баланса в Redis только после commit.
2. **Идемпотентность списаний**: partial unique index `(user_id, idem_key) WHERE idem_key IS NOT NULL`.
3. **Идемпотентность «X дня»**: unique `(chat_id, day_msk, title)`.
4. **Пер-чат-пер-юзер стейт**: композитный PK/UNIQUE `(user_id, chat_id)` вместо глобального.
5. **NLP/embedding конвейер**: маркер `nlp_processed_at IS NULL` + отсутствие строки в `message_embeddings` как «очередь» прямо в PG; частые маленькие батчи по interval-триггеру; идемпотентный `merge()`.
6. **Alembic сырым SQL c IF NOT EXISTS** + one-shot migrations-контейнер, от успешного завершения которого зависят остальные сервисы.
7. **Best-effort Redis**: любая работа с Redis обёрнута так, что его падение не ломает домен.
8. **ivfflat**: не создавать индекс до массового backfill; lists = sqrt(rows).
9. Чего НЕ повторять: дубль `message_id`/`telegram_message_id`; запись сырого tg id в `reactions.message_id`; `Base.metadata.create_all` вместо начальной миграции (в новом проекте таблицы users/messages/reactions создавать миграцией №1); RQ-заглушка.
