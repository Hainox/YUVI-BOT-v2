# Отчёт: LLM-интеграция и NLP-сервис в эталоне xyloz_tg_bot

Источник: клон `https://github.com/Hainox/xyloz_tg_bot` (локально: `C:\Users\root\AppData\Local\Temp\claude\C--Users-root-Desktop-YUVI-BOT\327a8fc2-b8d8-4c65-9d50-2d3d3ac0d69d\scratchpad\xyloz_tg_bot`).
Стек: Python 3.11, aiogram >=3.27, SQLAlchemy + Alembic, PostgreSQL + pgvector, APScheduler, FastAPI (nlp/), openai SDK >=1.40.

---

## 1. LLM-клиент (`bot/services/ai_client.py`)

### 1.1. SDK и бэкенд

- **SDK:** официальный `openai` (синхронный `OpenAI`-клиент), поверх OpenAI-compatible бэкенда **OpenCode Zen Go**.
- **base_url:** env `OPENCODE_BASE_URL`, дефолт `https://opencode.ai/zen/go/v1` (`.rstrip("/")`).
- **API-ключ:** env `OPENCODE_API_KEY` — обязательный, при отсутствии `RuntimeError("OPENCODE_API_KEY не задан")`.
- Клиент — ленивый синглтон `get_client()` (module-level `_client`), с `default_headers`: `User-Agent`, `HTTP-Referer`, `X-Title` (нужно для OpenRouter-подобных прокси).
- Идентификаторы моделей имеют префикс `opencode-go/` (константа `OPENCODE_PREFIX`), перед вызовом срезается: `model.removeprefix("opencode-go/")`.

### 1.2. Ключевой паттерн: ВСЁ через стрим

Все вызовы идут стримом, «чтобы избежать Cloudflare 524 при медленных моделях». Не-стриминговый `call()` — это тот же `stream()` с пустым callback:

```python
def stream(user_prompt, model, on_delta, system_prompt=None, on_reasoning=None) -> str:
    response = client.chat.completions.create(
        model=model_id, messages=messages,
        temperature=0.0, max_tokens=MAX_OUTPUT_TOKENS, stream=True,
    )
    for chunk in response:
        delta = chunk.choices[0].delta
        if delta.content: content_parts.append(...); on_delta(piece)
        # reasoning-модели: разные провайдеры кладут в разные поля
        reasoning_piece = getattr(delta, "reasoning_content", None) or getattr(delta, "reasoning", None)
        if reasoning_piece and on_reasoning: on_reasoning(reasoning_piece)

def call(user_prompt, model, system_prompt=None) -> str:
    return stream(user_prompt, model, lambda _d: None, system_prompt)
```

### 1.3. Поддержка reasoning-моделей

- Дельты reasoning собираются отдельно и идут в опциональный callback `on_reasoning` (qwen3.6, gpt-oss и т.п.).
- Если модель вернула ТОЛЬКО reasoning и пустой content — бросается `RuntimeError` с диагностической подсказкой: `"(reasoning_chars=N — модель ушла в reasoning, подними AI_MAX_OUTPUT_TOKENS или смени модель)"` + `finish_reason`.
- При исключении в середине стрима: если частичный content уже накоплен — возвращается он (graceful degradation), иначе — `RuntimeError`.

### 1.4. Лимиты/env клиента

| Env | Дефолт | Назначение |
|---|---|---|
| `OPENCODE_BASE_URL` | `https://opencode.ai/zen/go/v1` | base URL |
| `OPENCODE_API_KEY` | — (обязателен) | ключ |
| `AI_MAX_OUTPUT_TOKENS` | 16000 | `max_tokens` в запросе |
| `AI_CALL_TIMEOUT_SEC` | 300 | таймаут вызова |
| `AI_STREAM_TIMEOUT_SEC` | 300 | `timeout` у OpenAI-клиента |

Лимиты входа (в `bot/services/summary_service.py`, хелпер `_env()` читает по нескольким именам с fallback на старые `OPENROUTER_*`):

| Env | Дефолт | Назначение |
|---|---|---|
| `AI_MAX_MESSAGES` | 1000 | верхняя граница N в `/summary N` |
| `AI_MAX_INPUT_TOKENS` | 12000 | бюджет входного контекста |
| `AI_MAX_CHARS_PER_MESSAGE` | 800 | обрезка одного сообщения |
| `AI_MAX_CUSTOM_PROMPT_CHARS` | 1200 | лимит кастомного промпта `/sumc` |
| `SUMMARY_MODEL` | `opencode-go/qwen3.5-plus` | дефолтная модель |
| `AI_AVAILABLE_MODELS` | список из 7 моделей | что показывает `/model_list` |

Оценка токенов примитивная: `CHARS_PER_TOKEN = 4`, `_estimate_tokens(text) = len(text)//4`.

`DEFAULT_AVAILABLE_MODELS`: `opencode-go/qwen3.5-plus`, `qwen3.6-plus`, `deepseek-v4-flash`, `deepseek-v4-pro`, `mimo-v2.5-pro`, `kimi-k2.6`, `glm-5.1`.

---

## 2. Хранение модели и промптов; админ-команды

### 2.1. Таблица `bot_settings` (`common/models/bot_setting.py`)

Универсальный KV-стор:

```
bot_settings(key VARCHAR(100) PK, value TEXT NOT NULL, updated_by_tg_id BIGINT NULL, updated_at TIMESTAMP)
```

Использование ключей:
- `summary_model` — текущая LLM-модель (константа `SUMMARY_MODEL_KEY`);
- `summary_instruction` — кастомный system-промпт пересказа (`SUMMARY_PROMPT_KEY`);
- `joke:<UTC-date>` — кэш анекдота дня (глобальный);
- `phrase:<chat_id>:<UTC-date>` — кэш фразы дня per-chat.

Таблица создаётся лениво: `BotSetting.__table__.create(bind=engine, checkfirst=True)` перед каждым чтением (`_ensure_settings_table()`).

### 2.2. Файловые промпты (`common/prompts.py` + `config/prompts/*.md`)

Все системные/task-промпты — отдельные `.md` файлы, читаются с `lru_cache`:

```python
PROMPTS_DIR = Path(__file__).resolve().parent.parent / "config" / "prompts"
@lru_cache(maxsize=64)
def load(name: str) -> str: return (PROMPTS_DIR / f"{name}.md").read_text("utf-8").strip()
def render(name, **vars): return load(name).format(**vars)
```

14 файлов: `summary_system/task`, `digest_system/task`, `ask_system/task`, `ask_query_rewrite_system/task`, `user_card_system/task`, `joke_system/task`, `phrase_system/task`.

### 2.3. Админ-команды (`bot/handlers/messages.py`)

Проверка прав — `services/admin_service.py`: `is_admin_tg_id(tg_id)` по env `BOT_ADMIN_IDS` (CSV tg_id).

- `/model_show` → `get_summary_model()` (BotSetting `summary_model`, fallback env `SUMMARY_MODEL`).
- `/model_list` → `get_available_models()` (env `AI_AVAILABLE_MODELS` CSV или дефолтный список; текущая помечается «(текущая)»). Валидации, что модель из списка — НЕТ: `/model_set` принимает любую строку.
- `/model_set <модель>` → `set_summary_model(value, updated_by_tg_id)` — upsert в BotSetting.
- `/prompt_show` → `get_summary_instruction()` (BotSetting или `prompts.load("summary_system")`).
- `/prompt_set <текст>` → `set_summary_instruction()`.
- `/prompt_reset` → записывает в BotSetting дефолт из файла (не удаляет строку).

Команды регистрируются в двух scope (`bot/main.py`): `PUBLIC_COMMANDS` через `BotCommandScopeDefault`, `ADMIN_COMMANDS` (+model_*/prompt_*/admin_status/backfill/fb/farmwipe) через `BotCommandScopeAllChatAdministrators`.

---

## 3. Паттерн стриминга в Telegram (throttled edit)

Используется одинаково в `/summary`, `/digest`, `/ask`, `/card`. Суть: LLM-стрим работает в отдельном потоке (`asyncio.to_thread`), дельты складываются в **thread-safe `queue.Queue`** (две очереди: `content_q` и `reasoning_q`), а отдельная async-задача `updater()` раз в N секунд забирает всё накопленное и редактирует ОДНО прогресс-сообщение:

```python
content_q, reasoning_q = Queue(), Queue()
done = False
async def updater():
    while not done or not content_q.empty() or not reasoning_q.empty():
        changed = False
        while True:
            try: reasoning_text += reasoning_q.get_nowait(); changed = True
            except Empty: break
        while True:
            try: content_text += content_q.get_nowait(); changed = True
            except Empty: break
        if changed:
            candidate = (_format_content_preview(content_text) if content_text.strip()
                         else _format_reasoning_preview(reasoning_text))  # пока думает — хвост reasoning
            await push(candidate)
        await asyncio.sleep(UPDATE_INTERVAL_SEC)  # 3.0 сек — троттлинг

updater_task = asyncio.create_task(updater())
answer = await asyncio.to_thread(ai_client.stream, prompt, model, content_q.put, system, reasoning_q.put)
while not content_q.empty(): content_text += content_q.get_nowait()  # дочитать остатки
done = True; await updater_task
```

Детали:
- `UPDATE_INTERVAL_SEC = 3.0` (в `/summary` 2.5 при fallback, 1.0 при драфтах); `MAX_TG_TEXT = 3900`.
- Пока content пуст, показывается превью reasoning: `"Думаю над вопросом…\n\n…{хвост 400 символов}"` (`REASONING_TAIL_CHARS = 400`).
- `push()` дедуплицирует (`if text == last_pushed: return`), ловит `TelegramRetryAfter` (flood control → `await asyncio.sleep(exc.retry_after)`) и `_safe_edit` игнорирует `TelegramBadRequest "message is not modified"`.
- Финал: `_split_chunks(text, 3900)` — режет по `\n` (ищет `rfind("\n", i, end)`, принимает если `split_at > i+200`), первый чанк — edit прогресс-сообщения, остальные — новые сообщения.
- В `/summary` есть эксперимент с **Telegram drafts**: `bot.send_message_draft(chat_id, draft_id=msg.message_id, ...)`; при `TelegramBadRequest` с "TEXTDRAFT" — fallback на обычный edit-троттлинг (`_is_draft_unsupported`).
- В `/joke`, `/phrase` (без стрима, `ai_client.call`) — другой паттерн: тикер `_tick()` каждые 15 сек обновляет «Думаю… (N сек)», `asyncio.wait_for(coro, timeout=300)`.

---

## 4. Пайплайн `/ask` (RAG) — `bot/services/ask_service.py`, `bot/handlers/ask.py`

### 4.1. Хранение векторов

- Модель эмбеддингов: **`sentence-transformers/paraphrase-multilingual-mpnet-base-v2`** (768-dim, env `NLP_EMBED_MODEL`), крутится в контейнере nlp/, `normalize_embeddings=True`.
- Таблица `message_embeddings` (`common/models/message_embedding.py`): `message_id BIGINT PK FK→messages.id ON DELETE CASCADE`, `chat_id BIGINT indexed`, `embedding vector(768) NOT NULL` (тип `pgvector.sqlalchemy.Vector(768)`), `created_at`.
- Миграции: `20260513_02_pgvector.py` (`CREATE EXTENSION IF NOT EXISTS vector`, изначально vector(384) + ivfflat lists=100), затем `20260514_01_embedding_768.py` — пересоздание под 768 (drop+create, эмбеддинги уничтожаются, нужен re-backfill). ivfflat-индекс создают ПОСЛЕ backfill через `scripts/manage_pgvector_index.py create [--lists N]` (авто: `lists = max(10, sqrt(count))`); перед bulk-load индекс дропают.

### 4.2. Наполнение векторов — `bot/services/embed_worker.py`

- `embed_pending_once()`: выбирает сообщения без эмбеддинга (`~Message.id.in_(subquery MessageEmbedding.message_id)`), `len(text) >= EMBED_MIN_TEXT_LEN` (env, дефолт 10), батч `EMBED_WORKER_BATCH` (100), `order_by(Message.id.desc())` — свежие сначала.
- POST на `{NLP_SERVICE_URL}/embed/batch` (aiohttp, таймаут `NLP_EMBED_TIMEOUT_SEC` 120с), сохранение `session.merge(MessageEmbedding(...))`.
- Планировщик: каждые **45 сек** (job `embed_pending`, см. §7). Для истории — `scripts/embed_backfill.py` (цикл до опустошения, `--max-batches`) и `/backfill` в боте (`services/backfill_runner.py`: реестр jobs `{"embed": embed_pending_once, "nlp": classify_pending_once}`, остановка после 3 холостых итераций).

### 4.3. Поиск (гибрид: вектор + лексика)

`stream_ask(chat_id, query, on_delta, on_reasoning)` → `(header, llm_answer, results)`:

1. **Query rewrite:** `_rewrite_query()` — LLM (`ask_query_rewrite_system/task`) генерит 3 перефразировки (`ASK_REWRITE_VARIANTS=3`); при ошибке — только оригинал. Чистка нумерации/кавычек построчно.
2. **Эмбеддинг всех вариантов** одним батчем `POST /embed/batch`.
3. **Векторный поиск на каждый вариант** — `_search_similar()`: `MessageEmbedding.embedding.cosine_distance(query_vec)` (pgvector), join к Message/User, `filter(MessageEmbedding.chat_id == chat_id)`, `order_by(distance).limit(ASK_PER_QUERY_K=15)`; `similarity = 1.0 - dist`.
4. **Лексический поиск** — `_search_lexical()`: из вопроса извлекаются `@username` (`_extract_author`, regex `@([A-Za-z0-9_]{3,32})`) и значимые слова (`_keyword_terms`: длина ≥4, не из стоп-списка `_STOP`, max 6). Грубый стемминг `_term_root` (обрезать ≤2 буквы окончания) → `ILIKE '%корень%'` OR по корням; при наличии автора — фильтр `lower(User.username) == author` и `k×3`. Лексическим хитам присваивается искусственный `similarity`: `0.97 (автор-скоуп) / 0.80 + 0.01×число совпавших корней` — чтобы merge их не срезал.
5. **Merge** `_merge_hits()`: по `message_id` берётся max similarity, сортировка убыв., top `ASK_TOP_K=25`.
6. **Соседи** `_expand_with_neighbors()`: для каждого хита ±`ASK_NEIGHBORS_EACH_SIDE=2` сообщений по `created_at` (два запроса before/after), помечаются `is_hit=False`, итог сортируется хронологически.
7. **LLM-ответ**: `_format_context_for_llm()` — задача `ask_task` + вопрос + список строк `★[sim=0.87] 2026-05-01 14:32 МСК @user: текст` (хиты) / `· ...` (соседи), текст клиппится до 300 символов; system — `ask_system`. Модель — та же `get_summary_model()`. Формат ответа по промпту: секции «ЧТО НАЙДЕНО» (2–5 дословных цитат только из ★) и «РЕЗЮМЕ», до 1500 символов, без эмодзи.

Env: `ASK_TOP_K=25`, `ASK_NEIGHBORS_EACH_SIDE=2`, `ASK_REWRITE_VARIANTS=3`, `ASK_PER_QUERY_K=15`; `ASK_MIN_QUERY_LEN=3`. Время всегда конвертируется в МСК (`ZoneInfo("Europe/Moscow")`, naive datetime считается UTC).

---

## 5. Генеративные команды

### 5.1. `/summary`, `/sum`, `/summary_custom`, `/sumc` (`summary_service.py` + `handlers/messages.py`)

- `parse_summary_count`: `/summary N`, дефолт 20, диапазон 1..`AI_MAX_MESSAGES`.
- `get_recent_text_messages()`: последние N текстовых сообщений чата (desc→reverse), автор `@username|fullname|Unknown`, исключается само сообщение команды (`exclude_message_id`).
- `_fit_messages_to_token_budget()`: идёт с конца (свежие приоритетны), каждое сообщение клиппится до `AI_MAX_CHARS_PER_MESSAGE=800`, строка `- @author: текст`, суммарно ≤ `AI_MAX_INPUT_TOKENS=12000` (оценка len/4); в промпт добавляется `(Пропущено старых сообщений из-за лимита контекста: N)`.
- `/sumc N | промпт` (или `N промпт`, или просто промпт): кастомный фокус вставляется блоком «Дополнительный фокус от пользователя: … Если это противоречит данным чата, приоритет у фактов из сообщений.», клип до 1200 символов.
- system-промпт — редактируемый через `/prompt_set` (`summary_instruction`), task — `summary_task` (список по темам, 4–8 пунктов).

### 5.2. `/digest [days] [--debug]` + автодайджест (`digest_service.py`, 751 строка — самый сложный препроцессинг)

Не «скормить всё подряд», а **предвычисленная структура** для LLM:

- Период 1..30 дн., дефолт 7 (`parse_digest_days`).
- `_fetch_period_messages`: все текстовые за период + счётчик реакций (outerjoin `reactions`, `func.count`).
- **Bursts:** почасовые бакеты; порог `max(8, median×2.5)` (`BURST_MIN_HOUR_COUNT`, `BURST_MEDIAN_MULTIPLIER`); соседние часы сливаются; top-3 окна (`BURST_TOP_K`). Окна шире 2ч (`BURST_MAX_WIDTH_HOURS`) дробятся/сужаются по 10-минутным пикам (`_find_inner_peaks`, `_split_or_narrow_burst`: если 2 пика разнесены ≥60 мин — раздельные под-окна ±15 мин, иначе сужение до объединения пиков ±3 мин).
- Для каждого burst (`BurstContext`): top-5 авторов; пики плотности; **характерные униграммы/биграммы** burst-vs-фон (TF-ratio со сглаживанием: `ratio = (bf+α)/(gf+α)`, `α=1/(burst_total+bg_total)`; мин. частоты 4/2; стоп-словарь `RU_STOP` ~90 слов + regex слова ≥5 букв); **reply-цепочки** (top-4 root по числу ответов ≥2, до 6 ответов); **кандидаты в цитаты** (≥5 слов и ≥30 символов, сортировка по реакциям затем длине, top-8) — LLM разрешено цитировать ТОЛЬКО их, с ID `[Qn.k]`; **сэмпл** содержательных не-chain сообщений (15/окно, отсортированы по реакциям, выводятся хронологически).
- **Фон:** равномерная выборка 25 содержательных сообщений вне окон.
- Промпт: `digest_task` (алгоритм: биграммы → подтверждение через reply chains → формат «ВСПЛЕСКИ / ЦИТАТЫ / ВАЙБ», запреты на парафразы и выдумки) + заголовок `Период… Всего сообщений… Топ авторов` + блоки `=== BURST i ===`. При превышении `MAX_INPUT_TOKENS` строки откусываются с конца.
- `--debug` шлёт собранный промпт файлом (`BufferedInputFile`, `digest_debug_YYYYmmdd_HHMMSS.txt`).
- **Автодайджест:** APScheduler `CronTrigger(day_of_week="mon", hour=9, tz=Europe/Moscow)` — `_weekly_digest_job`: `find_active_chat_ids(window_days=14)` (distinct chat_id из messages), `has_data_for_period(chat_id, 7)`, `generate_digest()` (без стрима), рассылка чанками.

### 5.3. `/card [@user]` (`user_card_service.py`)

- `resolve_user_for_card`: приоритет reply > `@username` из аргумента > сам автор.
- Статистика: total, first/last message, avg длина, топ-5 полученных/поставленных реакций (join через `reactions`).
- Сэмпл: последние 200 сообщений (`CARD_MESSAGE_SAMPLE`), бюджет `0.7×MAX_INPUT_TOKENS` (с конца — свежие).
- Промпты `user_card_system/task`: секции «ОБРАЗ / ТЕМЫ / МАНЕРА / ЦИФРЫ», до 1200 символов, без выдумывания биографии. Стрим — тот же паттерн.

### 5.4. `/topics [days]` (`topics_service.py`)

- Сообщения ≥20 символов за 1..30 дн.; минимум 30, максимум 1500 (равномерное прореживание).
- Эмбеддинги батчем через `POST /embed/batch` → **KMeans** (`sklearn.cluster.KMeans`, `NUM_CLUSTERS=8`, `n_init=4`, `random_state=42`, `max_iter=100`), `actual_k = max(2, min(8, len))`.
- Кластеры < 4 сообщений отбрасываются (`MIN_CLUSTER_SIZE`). Для каждого кластера — 5 ближайших к центроиду текстов (косинус вручную через numpy).
- **LLM-лейблинг** одним вызовом `_label_clusters_with_llm`: inline-промпт «Для каждого назови тему 2-4 словами… ("разговоры"/"разное" — запрещено)… Ответ строго `CLUSTER 0: тема`», парсинг построчно; фолбэк-лейбл `Тема {cid}`.
- Вывод: `N. label (size сообщ.)` + топ-5 авторов + 3 примера. Без стрима (одно редактирование).

### 5.5. `/joke [--new]`, `/phrase [--new]` (`joke_service.py`, `phrase_service.py`)

- Оба кэшируются в `bot_settings` на день: `joke:<date>` (глобально), `phrase:<chat_id>:<date>` (per-chat). `--new` (только админ) форсит регенерацию.
- `/joke`: чистый `ai_client.call(prompts.load("joke_task"), model, prompts.load("joke_system"))` — без контекста чата; чистка кавычек.
- `/phrase`: контекст — до 80 сообщений (≥15 символов) за 24 часа, клип 200 символов; минимум 10 строк, иначе отказ. Промпт `phrase_system` содержит эталонные примеры стиля; берётся первая непустая строка ответа, чистятся кавычки/маркеры.

---

## 6. Контейнер nlp/ (FastAPI-инференс)

### 6.1. Состав (`nlp/main.py`, единственный файл кода)

- FastAPI `title="xyloz-nlp"`, lifespan загружает 3 модели в module-level dict `_state` (CPU, `device=-1`/`"cpu"`):
  - sentiment: **`seara/rubert-tiny2-russian-sentiment`** (env `NLP_SENTIMENT_MODEL`) — HF `pipeline("text-classification", top_k=None, truncation=True, max_length=NLP_MAX_LENGTH=256)`;
  - toxicity: **`cointegrated/rubert-tiny-toxicity`** (env `NLP_TOXICITY_MODEL`) — аналогично;
  - embed: **`sentence-transformers/paraphrase-multilingual-mpnet-base-v2`** (env `NLP_EMBED_MODEL`), `max_seq_length = NLP_EMBED_MAX_LENGTH=512`.
- Env: `NLP_BATCH_SIZE=32` (инференс классификации), `NLP_EMBED_BATCH_SIZE=64`.

### 6.2. Роуты

- `GET /health` → `{"status":"ok","models_loaded":bool}`; docker healthcheck дергает его (interval 30s, start_period 90s).
- `POST /classify/batch` `{texts:[...]}` → `{results:[{sentiment_label, sentiment_score, toxicity_score}]}`. Всё под `torch.no_grad()`. Нормализация: label → positive/negative/neutral (`_normalize_sentiment_label` по подстрокам pos/neg/neu); **знаковый score**: `+conf` для positive, `−conf` для negative, `0.0` для neutral. Toxicity: `1 − P(non-toxic)` (модель отдаёт 5 классов: non-toxic, insult, obscenity, threat, dangerous); fallback — сумма токсичных вероятностей, клип 0..1. Пустой текст → пустой `ClassifyResult`.
- `POST /embed/batch` `{texts:[...]}` → `{embeddings:[[float×768]], dim:768}` (`normalize_embeddings=True`, `convert_to_numpy=True`).

### 6.3. Dockerfile / развёртывание

- `python:3.11-slim` + build-essential; **torch ставится отдельно с CPU-индексом** `pip install --index-url https://download.pytorch.org/whl/cpu torch` (экономия образа); зависимости: fastapi, uvicorn[standard], pydantic>=2.5, transformers>=4.40, torch>=2.2, sentencepiece, protobuf, sentence-transformers>=2.7.
- `ENV HF_HOME=/app/.cache/huggingface`; в compose volume `hf_cache:/app/.cache/huggingface` — кэш моделей переживает пересборку. Порт 8000 (наружу 8001). `CMD uvicorn nlp.main:app`.
- Бот обращается по `NLP_SERVICE_URL=http://nlp:8000` (задано в compose environment).

---

## 7. Батч-классификация и планировщик (`bot/services/nlp_classifier.py`, `scheduler.py`)

- В `messages` добавлены колонки (миграция `20260513_01_nlp_fields.py`): `sentiment_score Float`, `sentiment_label String(20)`, `toxicity_score Float`, `topic_id Integer` (не используется), `nlp_processed_at DateTime`; индексы `idx_nlp_unprocessed(nlp_processed_at)`, `idx_chat_sentiment(chat_id, sentiment_label)`.
- `classify_pending_once()`: выборка `nlp_processed_at IS NULL AND text != ''`, батч `NLP_WORKER_BATCH=200`, `order_by(Message.id.desc())`; POST `/classify/batch` (таймаут `NLP_HTTP_TIMEOUT_SEC=60`); результаты пишутся `update(Message).values(sentiment_label=…, sentiment_score=…, toxicity_score=…, nlp_processed_at=now)`. При расхождении длин — предупреждение и zip по меньшей.
- Планировщик — `AsyncIOScheduler(timezone=Europe/Moscow)` внутри процесса бота (`start_scheduler(bot)` из `main()`), а НЕ отдельный воркер (каталог `worker/` — заглушка RQ, реально не используется). Jobs (все `coalesce=True, max_instances=1`):
  - `nlp_classify_pending` — IntervalTrigger **30 сек**;
  - `embed_pending` — IntervalTrigger **45 сек**;
  - `weekly_digest` — Cron пн 09:00 МСК;
  - остальные — не про AI (nominations 10:00, markets, tags).
- Исторический backfill: `scripts/nlp_backfill.py`, `scripts/embed_backfill.py`, `scripts/check_embeddings_coverage.py`, `scripts/debug_ask_recall.py`; из бота — админ-команда `/backfill start embed|nlp` (`backfill_runner.py` + `handlers/admin_status.py`).

## 8. `/mood [days]`, `/toxic [days]` (`bot/services/mood_service.py`, `handlers/mood.py`)

Чистый SQL по предвычисленным колонкам, без LLM:
- `/mood`: группировка `sentiment_label` за 1..90 дн. (дефолт 7) → количества и проценты positive/neutral/negative + «ещё N ждут классификации» (label IS NULL); топ-5 авторов по числу positive и negative сообщений.
- `/toxic`: порог `TOXIC_THRESHOLD=0.5`; доля токсичных от классифицированных; топ-5 авторов по СРЕДНЕЙ toxicity_score с `HAVING count>=5`; топ-5 самых токсичных сообщений с текстом (клип 120) и score.

## 9. Прочие AI-потребители

- `bot/services/feedback_ai_service.py` — саппорт-бот Mini App: system-промпт с «грунтингом» (список фич продукта, чтобы не галлюцинировал), требует СТРОГО JSON `{"reply": "...", "register": {"kind":"bug|idea","text":"..."}|null}`; парсер `_parse()` срезает ```json-fence и берёт `re.search(r"\{.*\}", s, re.S)`; любой сбой → graceful деградация на фолбэк-форму.
- Везде модель одна и та же — `get_summary_model()`; отдельных моделей per-фича нет.

## 10. Что стоит заимствовать для Yuvi Bot v2

1. **ai_client как единственная точка входа**: всегда stream + `on_delta`/`on_reasoning` callbacks, `call()` = stream с пустым callback; частичный content при обрыве; диагностика «модель ушла в reasoning».
2. **Двухочередной updater-паттерн** (content_q/reasoning_q + edit раз в 3 сек + RetryAfter + «message is not modified» + чанкование по 3900 с разрезом по \n).
3. **Промпты в .md-файлах** + lru_cache + переопределение через KV-таблицу `bot_settings` (`/prompt_set`, `/model_set` без рестарта).
4. **Гибридный RAG**: LLM-перефразировки ×3 → батч-эмбеддинг → pgvector cosine per-вариант → merge по max sim → лексический ILIKE-канал с приоритетом @author → соседи ±2 → промпт с маркерами ★/· и правилом «цитировать только ★».
5. **Digest-препроцессинг**: burst-окна по медиане, TF-vs-фон n-граммы, reply-цепочки, белый список цитат `[Qn.k]` — резко снижает галлюцинации на длинных периодах; `--debug` с выгрузкой промпта файлом.
6. **nlp/ как отдельный CPU-контейнер** c 3 моделями и батч-эндпоинтами; HF-кэш в volume; классификация — фоновыми джобами APScheduler (200/30с и 100/45с), результаты — в колонки `messages`, эмбеддинги — в `message_embeddings vector(768)`; ivfflat-индекс создавать после backfill.
