# VPN-подсистема в эталоне xyloz_tg_bot: полный инвентарь и чек-лист вырезания

> Зона анализа: всё VPN-связанное в эталонном репозитории `Heide172/xyloz_tg_bot`,
> подлежащее **полному исключению** из Yuvi Bot v2.
>
> Анализ проведён по **актуальному main** (коммит `5051a007751e7cb295a57d0d72312c52d640e09b`,
> "feat(duel): релевантные стикеры в муте бота"). Важно: VPN-код есть **только в main**;
> в ветке `dev` (729f6f5) его нет вообще. Локальная копия для анализа:
> `C:\Users\root\AppData\Local\Temp\claude\C--Users-root-Desktop-YUVI-BOT\327a8fc2-b8d8-4c65-9d50-2d3d3ac0d69d\scratchpad\xyloz_main`.

---

## 1. Что это такое (чтобы понимать, что именно выкидываем)

Подсистема `vpndigest` — это **отдельный userbot-мониторинг чужих VPN-чатов** (владелец бота
держит VPN-сервис для обхода блокировок). Pyrogram-клиент под **отдельным юзер-аккаунтом**
(не ботом) слушает список чатов, складывает сообщения в изолированные таблицы `vpn_*`,
а раз в сутки map-reduce-саммаризацией через LLM формирует дайджест и шлёт его юзер-аккаунтом
в «Избранное» или канал. К игровой/статистической функциональности бота **никак не относится**.

Появилась в истории коммитами:
- `fddb1a4` — feat(vpndigest): мониторинг VPN-чатов userbot'ом + дайджесты
- `7aecabb` — feat(vpndigest): поддержка форум-тем через kurigram
- `da73584` — perf(vpndigest): параллельная саммаризация топиков + лимит топ-N

---

## 2. Полный инвентарь VPN-артефактов

### 2.1. Пакет `vpndigest/` (14 файлов, ~30 КБ — вырезается целиком)

| Файл | Назначение |
|---|---|
| `vpndigest/__init__.py` | докстринг: описывает интеграцию с common.db / common.logger / bot.services.ai_client |
| `vpndigest/config.py` | все env-переменные подсистемы (см. §2.5), `load_dotenv` из корня проекта |
| `vpndigest/client.py` | `build_client(name="vpn_digest_userbot")` — Pyrogram `Client` с `session_string=VPN_SESSION_STRING`, `in_memory=True` |
| `vpndigest/login.py` | одноразовый интерактивный логин → печатает `VPN_SESSION_STRING` (запуск: `python -m vpndigest.login`) |
| `vpndigest/ingest.py` | `normalize(m: TgMessage) -> dict` и `run_listener()` — live-листенер `@app.on_message(filters.chat(chat_ids))` (entrypoint сервиса `vpn_userbot`) |
| `vpndigest/backfill.py` | подтяжка истории monitored-чатов за `VPN_BACKFILL_DAYS` (`python -m vpndigest.backfill --days N`), батчи по 200, прогрев кэша пиров через `get_dialogs()` |
| `vpndigest/topics.py` | кэш названий форум-тем `chat_id -> {topic_id: title}` через `app.get_forum_topics` (kurigram) |
| `vpndigest/storage.py` | `session_scope()`, `store_messages()` (bulk upsert `pg_insert(...).on_conflict_do_update`), `register_chat()`, `fetch_window()`, `chat_titles()` |
| `vpndigest/grouping.py` | `_NOISE`-фильтр («спс», «+1», эмодзи…), `TopicBucket` (dataclass), `group_into_topics()` (мин. 3 осмысленных сообщения на топик), `chats_summary()` |
| `vpndigest/summarize.py` | `summarize_topic()` и `make_tldr()` через **общий** `bot.services.ai_client.call(user, model, system)`; обрезка диалога до 12000 символов (хвост) |
| `vpndigest/prompts.py` | `TOPIC_SYSTEM`, `TOPIC_USER_TEMPLATE`, `TLDR_SYSTEM`, `TLDR_USER_TEMPLATE` — русскоязычные промпты «для владельца VPN-сервиса» |
| `vpndigest/publish.py` | `deliver(content)` — отправка юзер-аккаунтом в `VPN_DIGEST_TARGET_CHAT`, чанкование по 4000 символов |
| `vpndigest/worker.py` | `run_once()` (map-reduce: `ThreadPoolExecutor(max_workers=VPN_DIGEST_CONCURRENCY)` по топикам → TL;DR → сохранение `VpnDigest` → `deliver`), `run_scheduler()` — `BlockingScheduler` + `CronTrigger.from_crontab(VPN_DIGEST_CRON)` (entrypoint сервиса `vpn_digest_worker`) |
| `vpndigest/README.md` | инструкция первого запуска (login → env → alembic → backfill → worker --once → compose up) |

### 2.2. Модели SQLAlchemy в общем коде `common/models/`

Вырезать 3 файла:
- `common/models/vpn_monitored_chat.py` — класс `VpnMonitoredChat`, таблица `vpn_monitored_chats`
- `common/models/vpn_message.py` — класс `VpnMessage`, таблица `vpn_messages`
- `common/models/vpn_digest.py` — класс `VpnDigest`, таблица `vpn_digests`

И почистить `common/models/__init__.py` (строки 7–9 и 13):

```python
from common.models.vpn_monitored_chat import VpnMonitoredChat   # <- удалить
from common.models.vpn_message import VpnMessage                 # <- удалить
from common.models.vpn_digest import VpnDigest                   # <- удалить

__all__ = [
    'User', 'Message', 'Reaction', 'BotSetting', 'DailyPick',
    'VpnMonitoredChat', 'VpnMessage', 'VpnDigest',               # <- удалить 2-ю строку
]
```

### 2.3. Таблицы БД и миграция

Миграция: `migrations/versions/20260603_01_vpn_digest.py`
(`revision = "20260603_01"`, `down_revision = "20260527_01"` — twin_state).

Создаёт 3 таблицы и 4 индекса:

| Таблица | Колонки |
|---|---|
| `vpn_monitored_chats` | `id` BIGINT PK (tg chat_id -100…), `title` VARCHAR(255), `username` VARCHAR(255), `is_forum` BOOL, `enabled` BOOL, `added_at` DATETIME |
| `vpn_messages` | `id` BIGINT PK autoincrement, `chat_id` BIGINT NOT NULL, `telegram_message_id` BIGINT NOT NULL, `user_id` BIGINT, `username` VARCHAR(255), `text` TEXT, `reply_to` BIGINT, `topic_id` BIGINT (message_thread_id; NULL = General), `topic_title` VARCHAR(255), `is_forwarded` BOOL, `has_media` BOOL, `created_at` DATETIME NOT NULL (UTC-naive), `edited_at` DATETIME |
| `vpn_digests` | `id` BIGINT PK autoincrement, `period_start`/`period_end` DATETIME NOT NULL, `chat_id` BIGINT (NULL = общий), `content` TEXT (markdown), `model` VARCHAR(100), `messages_count` INT, `delivered` BOOL, `created_at` DATETIME |

Индексы: `uq_vpn_chat_tg_message` (UNIQUE chat_id+telegram_message_id — идемпотентный upsert),
`idx_vpn_chat_created`, `idx_vpn_chat_topic_created`, `idx_vpn_digest_period_end`.

Плюс импорты в `migrations/env.py`, строки 18–20:

```python
from common.models.vpn_monitored_chat import VpnMonitoredChat   # <- удалить
from common.models.vpn_message import VpnMessage                 # <- удалить
from common.models.vpn_digest import VpnDigest                   # <- удалить
```

### 2.4. docker-compose.yml — два сервиса (строки 111–141)

```yaml
  # --- vpn-digest: мониторинг VPN-чатов userbot'ом + дайджесты ---
  vpn_userbot:
    build: { context: ., dockerfile: bot.Dockerfile }   # общий образ бота, своего Dockerfile нет
    command: python -m vpndigest.ingest
    env_file: [./.env]
    depends_on: { migrations: { condition: service_completed_successfully } }
    restart: unless-stopped
    networks: [default, dokploy-network]

  vpn_digest_worker:
    build: { context: ., dockerfile: bot.Dockerfile }
    command: python -m vpndigest.worker
    ...(идентично)
```

Оба сервиса собираются из общего `bot.Dockerfile` (`COPY . .`, `PYTHONPATH=/app`) —
никакой другой сервис от них **не зависит** (`depends_on` на них нигде нет), удаление блоков безопасно.

### 2.5. Переменные окружения

Чисто VPN-овые (удалить из `.env` / деплой-секретов, нигде больше не используются):

| Переменная | Дефолт | Где читается |
|---|---|---|
| `VPN_SESSION_STRING` | `""` | `vpndigest/config.py:32` (строковая Pyrogram-сессия отдельного аккаунта) |
| `VPN_MONITORED_CHAT_IDS` | `""` | `config.py` `monitored_chat_ids()` — csv из `-100…` id или `@username` |
| `VPN_DIGEST_TARGET_CHAT` | `"me"` (Избранное) | `config.py:39` |
| `VPN_BACKFILL_DAYS` | `7` | `config.py:42` |
| `VPN_DIGEST_MODEL` | `opencode-go/qwen3.5-plus` | `config.py:45` |
| `VPN_DIGEST_MAX_TOPICS` | `15` | `config.py:48` |
| `VPN_DIGEST_CONCURRENCY` | `6` | `config.py:50` |
| `VPN_DIGEST_CRON` | `0 9 * * *` | `config.py:53` |
| `VPN_DIGEST_WINDOW_HOURS` | `24` | `config.py:54` |

Общие (НЕ трогать — используются вне VPN):
- `TG_API_ID` / `TG_API_HASH` — также нужны `scripts/history_load.py` (импорт истории основного чата — это ядро фичи «100% сообщений»).
- `TZ` — общесистемная (`VPN_TZ = os.getenv("TZ", "Europe/Moscow")` — просто алиас).
- `OPENCODE_API_KEY`, `OPENCODE_BASE_URL` — общий `ai_client`.

### 2.6. Mini App (SvelteKit)

`miniapp/src/routes/+page.svelte`, строки 70–75: плитка главного меню с **внешней ссылкой**
на сайт поддержки VPN-сервиса (punycode-домен `https://xn--b1afabzvcegckfhg.xn--p1ai/`):

```js
{
  href: 'https://xn--b1afabzvcegckfhg.xn--p1ai/',
  title: 'Поддержка по VPN',
  desc: 'Помощь с доступом',
  external: true
}
```

Удалить элемент из массива `baseTiles`. Отдельного роута `/vpn` в miniapp **нет** —
это единственное VPN-место во фронтенде.

Ложное срабатывание: `miniapp/package-lock.json` — подстрока `vpn` внутри base64
`integrity`-хэша (строка 952, `sha512-Sk/uYFOBAB7…`), не трогать.

### 2.7. Где VPN-кода НЕТ (проверено grep-ом по всему дереву)

- `bot/` (хендлеры, команды, `bot/services/scheduler.py`, `bot/main.py`) — **0 упоминаний**. Ни одной команды/хендлера, связанных с VPN, у бота нет.
- `api/` (FastAPI Mini App backend) — 0.
- `worker/` (основной воркер) — 0.
- `nlp/`, `cobalt/`, `docs/`, `config/prompts/`, `README.md` (корневой), `alembic.ini`, `.gitignore`, `scripts/` — 0.
- Scheduler-задач VPN в основном боте нет: свой шедулинг vpndigest держит в собственном процессе (`vpndigest/worker.py: BlockingScheduler`).

---

## 3. Граф зависимостей: как переплетено с общим кодом

**Направление строго одностороннее: `vpndigest` -> общие модули. Обратных зависимостей нет.**

Что импортирует vpndigest из общего кода:

| Общий модуль | Кто использует | Комментарий |
|---|---|---|
| `common.db.base.Base` | vpn-модели | декларативная база |
| `common.db.db.SessionLocal` | `vpndigest/storage.py` | сессии БД |
| `common.logger.get_logger` | ingest/backfill/topics/publish/worker | логирование |
| `common.models` (`VpnMessage`, `VpnMonitoredChat`, `VpnDigest`) | storage/grouping/worker | сами вырезаются |
| `bot.services.ai_client.call(user_prompt, model, system_prompt)` | `vpndigest/summarize.py` | общий LLM-клиент (OpenCode, OpenAI-compatible) |

Что в общем коде знает про VPN (полный список «щупалец», все — тривиальные):
1. `common/models/__init__.py` — 3 импорта + 1 строка `__all__`.
2. `migrations/env.py` — 3 импорта (нужны только чтобы модели попали в `Base.metadata` для alembic).
3. `docker-compose.yml` — 2 сервиса.
4. `miniapp/src/routes/+page.svelte` — 1 плитка меню.
5. Цепочка alembic-миграций — см. ниже, **единственное настоящее переплетение**.

### 3.1. ⚠️ Главная ловушка: VPN-миграция стоит В СЕРЕДИНЕ цепочки

```
… → 20260526_01 → 20260527_01 (twin_state) → 20260603_01 (vpn_digest) → 20260616_01 (gacha_visual) → 20260616_02 (gacha_v2) → 20260616_03 (gacha_team)
```

`migrations/versions/20260616_01_gacha_visual.py:11` содержит `down_revision = "20260603_01"`.
Просто удалить файл `20260603_01_vpn_digest.py` нельзя — alembic сломается
(«revision not found»). При переносе миграций в новый проект:
**удалить файл VPN-миграции и перевесить в `20260616_01_gacha_visual.py`
`down_revision = "20260527_01"`.**

### 3.2. Зависимости в requirements.txt — ничего удалять НЕ надо

VPN-подсистема сознательно не добавила ни одной своей зависимости (это прямо написано в её README).
Все кандидаты «на выпил» на деле общие:

| Пакет | Кажется VPN-овым? | Реально используется ещё |
|---|---|---|
| `kurigram>=2.1` (форк Pyrogram) + `tgcrypto` | да | `scripts/history_load.py` — userbot-импорт истории ОСНОВНОГО чата (критично для фичи «100% сообщений») |
| `APScheduler>=3.10` | да | `bot/services/scheduler.py` (`AsyncIOScheduler` основного бота) |
| `openai` | нет | общий `bot/services/ai_client.py` |
| `tqdm` | нет | `scripts/history_load.py` |

Итог: `requirements.txt` при вырезании VPN не меняется вообще.

---

## 4. Чек-лист «что вырезать» (= что НЕ переносить в Yuvi Bot v2)

- [ ] **Каталог `vpndigest/` целиком** (14 файлов).
- [ ] **`common/models/vpn_monitored_chat.py`, `common/models/vpn_message.py`, `common/models/vpn_digest.py`**.
- [ ] **`common/models/__init__.py`**: убрать 3 импорта `Vpn*` и `'VpnMonitoredChat', 'VpnMessage', 'VpnDigest',` из `__all__`.
- [ ] **`migrations/env.py`**: убрать 3 импорта `Vpn*` (строки 18–20).
- [ ] **`migrations/versions/20260603_01_vpn_digest.py`**: не переносить; если копируется цепочка миграций — перевесить `down_revision` у `20260616_01_gacha_visual.py` с `"20260603_01"` на `"20260527_01"`.
- [ ] **`docker-compose.yml`**: не переносить сервисы `vpn_userbot` и `vpn_digest_worker` (строки 111–141) вместе с комментарием-заголовком.
- [ ] **`.env` / секреты деплоя**: не заводить `VPN_SESSION_STRING`, `VPN_MONITORED_CHAT_IDS`, `VPN_DIGEST_TARGET_CHAT`, `VPN_DIGEST_CRON`, `VPN_DIGEST_WINDOW_HOURS`, `VPN_BACKFILL_DAYS`, `VPN_DIGEST_MODEL`, `VPN_DIGEST_MAX_TOPICS`, `VPN_DIGEST_CONCURRENCY`.
- [ ] **Miniapp**: не переносить плитку `«Поддержка по VPN»` (объект с `href: 'https://xn--b1afabzvcegckfhg.xn--p1ai/'`) из `baseTiles` в `miniapp/src/routes/+page.svelte`.
- [ ] **БД**: не создавать таблицы `vpn_monitored_chats`, `vpn_messages`, `vpn_digests` (в новом проекте просто не будет миграции; в живой БД эталона их снёс бы `downgrade()`).
- [ ] **requirements.txt**: оставить как есть (`kurigram`, `tgcrypto`, `APScheduler`, `tqdm`, `openai` — общие).
- [ ] **`TG_API_ID` / `TG_API_HASH`**: оставить — нужны для `scripts/history_load.py`.

### Проверка чистоты после вырезания

```bash
# 1) ни одного упоминания (кроме false-positive в package-lock.json):
grep -riIn --exclude=package-lock.json vpn .

# 2) компоуз валиден и не знает о vpn-сервисах:
docker compose config --services   # нет vpn_userbot / vpn_digest_worker

# 3) цепочка миграций не разорвана:
alembic history                    # непрерывна, без 20260603_01

# 4) импорты моделей живы:
python -c "import common.models; print(common.models.__all__)"
```

---

## 5. Оценка чистоты отреза

Вырезается **хирургически, без риска**: авторы изолировали подсистему образцово —
отдельный python-пакет, отдельные таблицы с префиксом `vpn_`, отдельные env с префиксом
`VPN_`, отдельные compose-сервисы без входящих `depends_on`, ноль своих зависимостей,
ноль упоминаний в хендлерах/шедулере/API. Единственные два места, требующие правки руками:
`common/models/__init__.py` + `migrations/env.py` (по 3 строки) и `down_revision`
в `20260616_01_gacha_visual.py`. Плюс одна плитка в miniapp.

## 6. Паттерны из vpndigest, которые стоит переиспользовать (в НЕ-VPN контексте)

Хотя сам VPN-функционал исключается, внутри — готовые образцы для фич Yuvi Bot v2
(сбор 100% сообщений и дайджесты своего чата):

1. **Идемпотентный bulk-upsert** (`vpndigest/storage.py`) — тот же паттерн пригоден для приёма сообщений без дублей:
```python
stmt = pg_insert(VpnMessage).values(rows)
stmt = stmt.on_conflict_do_update(
    index_elements=["chat_id", "telegram_message_id"],
    set_={"text": stmt.excluded.text, "edited_at": stmt.excluded.edited_at, ...},
)
```
2. **Map-reduce дайджест** (`worker.py`): параллельная саммаризация топиков в `ThreadPoolExecutor` (блокирующий `ai_client.call` в потоках) + reduce в TL;DR + лимит топ-N топиков по активности.
3. **Noise-фильтр** (`grouping.py`): множество `_NOISE` + `len(t) <= 2` + минимум 3 осмысленных сообщения на топик — дешёвая предфильтрация перед LLM.
4. **Строковая Pyrogram-сессия** (`login.py` → `client.py` с `in_memory=True`): деплой userbot-а без файлов `*.session` в контейнере — пригодится для `history_load`-механики.
5. **Кэш форум-топиков** (`topics.py`): один вызов `get_forum_topics` на чат вместо запроса на каждое сообщение.
6. **Чанкование длинных сообщений** (`publish.py:_chunks`, 4000 символов по границам строк) — для любых длинных ответов бота.
