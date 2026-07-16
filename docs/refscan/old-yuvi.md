# Отчёт: старый Yuvi-bot (источник идей фич для Yuvi Bot v2)

> Зона анализа: репозиторий старого заброшенного Yuvi-bot целиком.
> Локальный клон: `C:\Users\root\AppData\Local\Temp\old-yuvi`
> GitHub: `https://github.com/Hainox/Yuvi-bot.git` (рабочая ветка `docs/phase-1-context`, см. RESUME.md).
> Код НЕ переносится в v2 — только идеи и проверенные паттерны.

---

## 1. Общая картина

**Что это:** Telegram-бот для русскоязычного чата друзей: тихий сбор 100% сообщений, статистика, 8 юмористических номинаций дня (`/awards`), лотерея (`/yuvi`), AI-команды через OpenRouter, экономика «тугриков», казино-слот Mini App (React + aiohttp).

**Стек (requirements.txt, точные версии):**
- `aiogram==3.27.0` (polling, не webhook)
- `sqlalchemy[asyncio]==2.0.49` + `asyncpg==0.31.0` + `alembic==1.18.4` (PostgreSQL)
- `pydantic-settings==2.14.0`
- `APScheduler==3.11.2`
- `openai==1.77.0` (клиент к OpenRouter, ранее был Gemini/Groq)
- `aiohttp==3.10.11` (Steam API + Mini App сервер)
- `structlog==25.5.0`
- `pymorphy3==2.0.6` (детекция мата; в коде импортируется как `import pymorphy3 as pymorphy2`)
- Python 3.11 обязательно (pymorphy2-словари несовместимы с 3.12+, см. RESUME.md)

**ВАЖНО — в репо ДВЕ генерации кода:**
- **Активная** (запускается через корневой `main.py`): `main.py`, `config.py`, `bot/middleware/` (collector.py, cooldown.py, db_session.py), `bot/handlers/`, `bot/services/*_service.py` + `slot_engine.py`, `bot/api/`, корневой `db/` (models.py, session.py, queries.py, `*_queries.py`, crud_slot.py).
- **Мёртвая legacy** (первая попытка, никем не импортируется из активного main.py): `bot/main.py`, `bot/config.py`, `bot/middlewares/db.py`, `bot/db/` (base.py, crud.py, models.py), `bot/handlers/messages.py`, `bot/services/{awards,lottery,stats,summary,steam}.py`, `bot/utils/profanity.py`. При изучении игнорировать.

### Структура (активная часть)
```
old-yuvi/
├── main.py                  # точка входа: middleware, роутеры, error handler, scheduler, API server
├── config.py                # pydantic-settings
├── alembic.ini, alembic/    # + db/migrations/ (versions: 0001_initial_schema, 0002_bot_settings,
│                            #   0003_economy_core, d751734f20fe_add_slot_tables)
├── data/profanity_ru.txt    # 64 строки: словарь матерных лемм (комментарии через #)
├── bot/
│   ├── middleware/          # collector.py, cooldown.py, db_session.py
│   ├── handlers/            # commands, stats, awards, yuvi, ai, admin, casino, economy
│   ├── services/            # awards_service, yuvi_service, stats_service, ai_service,
│   │                        # gemini_service (OpenRouter), steam_service, profanity_service,
│   │                        # economy_service, slot_engine
│   ├── constants/           # awards.py (тексты номинаций), ai.py (модели/лимиты)
│   └── api/                 # server.py (aiohttp), routes.py, auth.py (initData HMAC)
├── db/
│   ├── models.py, session.py, queries.py
│   ├── stats_queries.py, awards_queries.py, lottery_queries.py,
│   │   ai_queries.py, settings_queries.py, economy_queries.py, crud_slot.py
├── webapp/                  # React 18 UMD + Babel standalone Mini App (слот 3×5)
└── tests/                   # 155 тестов в 14 файлах
```

### Переменные окружения (config.py, класс `Settings(BaseSettings)`, env_file=".env")
| Переменная | Тип | Назначение |
|---|---|---|
| `BOT_TOKEN` | `SecretStr`, обязат. | токен бота |
| `DATABASE_URL` | `str`, обязат. | `postgresql+asyncpg://user:pass@host:5432/db` |
| `OPENROUTER_API_KEY` | `SecretStr`, обязат. | LLM |
| `CHAT_ID` | `int`, обязат. | единственный разрешённый чат (D-06) |
| `SLOT_WEBAPP_URL` | `str\|None` | публичный HTTPS URL Mini App |
| `SLOT_API_HOST` / `SLOT_API_PORT` | `0.0.0.0` / `8080` | aiohttp за nginx |
| `SLOT_ENABLE_CORS` | `bool=False` | CORS только если фронт на другом домене |
| `SLOT_WEBAPP_DIR` | `str\|None` | путь к webapp/ (auto-detect) |

Критичный setup (CLAUDE.md): BotFather → Group Privacy **Disabled**, бот — админ группы, иначе не видит сообщения.

---

## 2. Точка входа и порядок middleware (main.py) — КЛЮЧЕВОЙ ПАТТЕРН

Порядок регистрации критичен и прокомментирован в коде:

```python
dp = Dispatcher()
# 1) Сессия БД — на UPDATE-уровне (покрывает все типы апдейтов)
dp.update.middleware(DbSessionMiddleware())
# 2) Сборщик — OUTER на message-уровне: видит ВСЕ сообщения, даже без handler'а
dp.message.outer_middleware(CollectorMiddleware())
# 3) Cooldown — INNER (не outer!), чтобы команда успела записаться в статистику
dp.message.middleware(CooldownMiddleware(commands=[
    "stats", "top", "words", "activity", "awards", "yuvi",
    "summary", "summary_custom", "digest", "card", "mood", "toxic",
    "topics", "ask", "balance", "leaderboard", "economy",
]))
# 4) Фильтр чата на уровне всего message-роутера + личка для отладки
dp.message.filter((F.chat.id == settings.chat_id) | (F.chat.type == "private"))
```

Дальше: `dp.include_router(...)` для 8 роутеров; глобальный `@dp.error()` — отдельная ветка для `TelegramRetryAfter` (лог + return, без падения); `scheduler.start()`; `await start_api_server(session_factory)` (aiohttp в том же event loop); `dp.start_polling(bot, drop_pending_updates=True, allowed_updates=["message","chat_member","my_chat_member"])`; в `finally` — cleanup API runner, `scheduler.shutdown()`, `bot.session.close()`.

Логирование — structlog: `structlog.contextvars.merge_contextvars` → `add_log_level` → `TimeStamper(fmt="iso")` → `ConsoleRenderer()`; асинхронные вызовы `await log.ainfo("event_name", key=value)`.

### 2.1 DbSessionMiddleware (bot/middleware/db_session.py) — 25 строк, целиком

```python
class DbSessionMiddleware(BaseMiddleware):
    async def __call__(self, handler, event: TelegramObject, data: dict) -> Any:
        async with session_factory() as session:
            data["session"] = session
            return await handler(event, data)
```
Handlers получают сессию как именованный аргумент aiogram-DI: `async def cmd_stats(message: Message, session: AsyncSession)`.

`db/session.py`:
```python
engine = create_async_engine(settings.database_url, pool_size=10, max_overflow=20,
                             pool_pre_ping=True, echo=False)
session_factory = async_sessionmaker(engine, class_=AsyncSession,
                                     expire_on_commit=False)  # критично: атрибуты доступны после commit
```

### 2.2 CollectorMiddleware (bot/middleware/collector.py) — сбор 100% сообщений

Порядок фильтров (каждый — «пропустить и передать дальше», не блокирует handler):
1. `event.chat.id != settings.chat_id` → skip (не собирать лички/чужие группы, WR-01);
2. `event.from_user is None` → skip (анонимные админы, D-11);
3. `event.from_user.is_bot or event.from_user.id == 777000` → skip (777000 = служебный аккаунт Telegram);
4. `_is_service_message(msg)` → skip. Функция проверяет 13 полей: `new_chat_members, left_chat_member, new_chat_title, new_chat_photo, delete_chat_photo, group_chat_created, supergroup_chat_created, channel_chat_created, pinned_message, video_chat_started, video_chat_ended, video_chat_scheduled, message_auto_delete_timer_changed`.

Извлечение признаков:
```python
if event.photo: msg_type = "photo"
elif event.animation: msg_type = "animation"
elif event.sticker: msg_type = "sticker"
else: msg_type = "text"                      # fallback: video/audio/doc/voice
is_forward = event.forward_origin is not None  # Bot API 7.0+; НЕ forward_from (устарел)
text = event.text or event.caption or ""
word_count = len(text.split()); char_count = len(text)
profanity_count = count_profanity(text)
```

Запись — три операции в ОДНОЙ транзакции, один `session.commit()`, при ошибке `rollback()` и всё равно `return await handler(event, data)` (сбор никогда не блокирует доставку команды):
`get_or_create_user` → `insert_message` → `upsert_daily_stats`.

### 2.3 CooldownMiddleware (bot/middleware/cooldown.py)

- Принимает `commands: list[str] | dict[str, int]` (одинаковый или пер-командный cooldown), дефолт `COOLDOWN_SECONDS = 30`.
- Хранилище in-memory: `self._last_used: dict[tuple[user_id, command], float]` по `time.monotonic()`; сбрасывается при рестарте — признано приемлемым (D-24).
- Парсинг команды: `text.lstrip("/").split()[0].split("@")[0].lower()` — обрабатывает `/stats@bot` и аргументы.
- При блокировке: `await event.answer(f"⏳ Подожди ещё {remaining} сек")` и `return` без вызова handler.

---

## 3. Модели БД (db/models.py, SQLAlchemy 2.0 Mapped-стиль)

| Таблица | Колонки (тип) | Ключи/индексы |
|---|---|---|
| `users` | `id` BigInteger PK (=tg user_id), `username` Text?, `first_name` Text NOT NULL, `last_name` Text?, `is_bot` Bool def False, `first_seen`/`last_seen` TIMESTAMP(tz) server_default now() | — |
| `messages` | `id` BigInteger PK autoincr, `message_id` BigInteger UNIQUE, `user_id` BigInteger, `sent_at` TIMESTAMP(tz), `text` Text? (None для media без caption), `word_count`/`char_count`/`profanity_count` Int def 0, `msg_type` Text, `is_forward` Bool | `idx_messages_sent_at(sent_at)`, `idx_messages_user_sent(user_id,sent_at)`, `idx_messages_type_sent(msg_type,sent_at)` |
| `daily_stats` | `id` PK, `user_id`, `stat_date` Date, `message_count`, `word_count`, `profanity_count`, `photo_count`, `forward_count`, `longest_msg_len` (все Int def 0) | `uq_daily_stats_user_date(user_id,stat_date)` UNIQUE, `idx_daily_stats_date(stat_date)` |
| `lottery_state` | `id` PK, `kind` Text UNIQUE ('yuvi' \| 'тугосеря'), `user_id` BigInteger, `granted_at`, `expires_at` TIMESTAMP(tz) | один ряд на kind |
| `bot_settings` | `key` Text PK, `value` Text, `updated_at` | KV-хранилище (модель/промпт LLM) |
| `wallets` | `user_id` BigInteger PK, `balance` Int def 0, `total_earned`, `total_spent`, `updated_at` (onupdate=now()) | — |
| `transactions` | `id` BigInteger PK, `user_id`, `amount` Int (+кредит/−дебет), `type` Text, `description` Text?, `ref_id` BigInteger?, `created_at` | `ix_transactions_user_created(user_id,created_at)` |
| `chat_bank` | `chat_id` BigInteger PK, `balance` Int, `total_collected` Int | аккумулирует 5% комиссии |
| `slot_balance` | `user_id` PK, `balance` def 2000, `free_spins`, `total_spins`, `total_wins`, `total_wagered`, `big_win_count`, created/updated | отдельный кошелёк казино(!) |
| `slot_history` | `id` PK, `user_id`, `chat_id?`, `kind` (bet\|win\|big_win\|free_win), `amount` (отриц. для ставки), `bet?`, `payout?`, `extra` Text? (JSON), `created_at` | `ix_slot_history_user_created` |

Архитектурная заметка: `slot_balance` и `wallets` — ДВЕ параллельные валюты, не связанные между собой. В v2 это надо объединить в одну гривну.

### 3.1 Ключевые upsert-паттерны (db/queries.py) — стоит заимствовать 1:1

```python
# users: ON CONFLICT (id) DO UPDATE, first_seen и is_bot не трогаем
stmt = pg_insert(User).values(id=..., username=..., ...)
stmt = stmt.on_conflict_do_update(index_elements=["id"], set_={
    "username": stmt.excluded.username, "first_name": stmt.excluded.first_name,
    "last_name": stmt.excluded.last_name, "last_seen": func.now()})

# messages: идемпотентность при повторной обработке update
.on_conflict_do_nothing(index_elements=["message_id"])

# daily_stats: инкрементальный агрегат, НЕ пересчёт из raw messages
stmt = stmt.on_conflict_do_update(constraint="uq_daily_stats_user_date", set_={
    "message_count": DailyStat.message_count + stmt.excluded.message_count,
    "word_count":    DailyStat.word_count + stmt.excluded.word_count,
    "profanity_count": DailyStat.profanity_count + stmt.excluded.profanity_count,
    "photo_count":   DailyStat.photo_count + stmt.excluded.photo_count,
    "forward_count": DailyStat.forward_count + stmt.excluded.forward_count,
    "longest_msg_len": func.greatest(DailyStat.longest_msg_len,
                                     stmt.excluded.longest_msg_len)})
```

---

## 4. /awards — 8 номинаций дня (главная фича)

Файлы: `bot/handlers/awards.py` → `bot/services/awards_service.py` → `db/awards_queries.py` + `bot/constants/awards.py`.

«Сегодня» = `get_today_msk()` = `datetime.now(tz=zoneinfo.ZoneInfo("Europe/Moscow")).date()`.

Один SELECT из `daily_stats JOIN users` за сегодня (`func.coalesce(User.first_name, User.username, "Участник").label("name")`), дальше победители считаются в Python по строкам:

| Ключ | Название (эмодзи) | Формула | Источник |
|---|---|---|---|
| `natural` | 👑 «Главный натурал» — больше всех писал | `max(message_count)`, только где `>0` | daily_stats |
| `profanity` | 🤬 «Эу — ты матершник блин» | `max(profanity_count)`, `>0` | daily_stats (детекция — §5) |
| `tugosera` | 💩 «Тугосеря дня» — случайная жертва | `random.choice(all_user_ids)` + фиксация в `lottery_state(kind='тугосеря')` до 23:59:59 MSK — статус живёт ~24ч, повторный вызов /awards возвращает того же | lottery_state |
| `photo` | 🖼️ «Ван Гог с AliExpress» | `max(photo_count)`, `>0` | daily_stats |
| `forward` | 👵 «Дряхлая бабка у подъезда» | `max(forward_count)`, `>0` | daily_stats |
| `steam` | 🎮 «Гифтек для админа» — игра дня из Steam Wishlist | случайная игра из wishlist (§6); карточка отправляется ВСЕГДА, при недоступности — «Steam недоступен 😢» | Steam API + TTL-кэш |
| `longest` | 📜 «Да все завали...» — самое длинное сообщение | `max(longest_msg_len)` (в daily_stats через GREATEST) | daily_stats |
| `spy` | 🕵️ «Украинский розвiдчiк» — наименее активный | `min(message_count)` среди писавших (`>=1`); при ничьей — `random.choice(tied)` (D-12) | daily_stats |

Хелпер выбора победителя:
```python
def winner(rows_list, key_fn):
    candidates = [r for r in rows_list if key_fn(r) > 0]
    if not candidates: return None
    r = max(candidates, key=key_fn)
    return {"name": r.name, "user_id": r.user_id, "count": key_fn(r)}
```

**Тугосеря — паттерн get-or-set через `lottery_state`** (`get_or_set_tugosera`): SELECT где `kind='тугосеря' AND expires_at > NOW()` → если есть, вернуть (без commit); иначе `random.choice(candidate_user_ids)`, `pg_insert(...).on_conflict_do_update(index_elements=["kind"], ...)` с `expires_at = 23:59:59` текущего дня MSK, commit. Это единственная write-функция модуля — остальные read-only.

**Оформление** (`bot/constants/awards.py`): `AWARD_DEFINITIONS` (emoji, title, description, count_emoji, count_suffix), `AWARD_COMMENTS` — по 3 случайных смешных комментария на номинацию, `AWARD_ORDER` — порядок вывода. Карточка:
```
{emoji} <b>{title}</b> — {description}

<b>{html.escape(name)}</b>
{count_emoji} {count} {suffix} {random.choice(comments)}
```
Все 8 карточек шлются ОДНИМ сообщением через `"\n\n".join(cards)` (не 8 отдельных — уходит проблема rate limit). Номинации без кандидатов пропускаются (D-11); если данных нет вообще — «Сегодня все молчат... наградить некого!». **КРИТИЧНО везде:** `html.escape(name)` — first_name может содержать `<`, `>`, `&` (parse_mode=HTML).

**Интеграция с экономикой** (уже была реализована — прямой прототип для v2): после отправки карточек handler собирает `get_winner_user_ids_today()` (6 «статистических» победителей + тугосеря; Steam — не человек, не входит), затем:
```python
amounts = {uid: random.choice(AWARD_AMOUNTS) for uid in winner_ids}  # AWARD_AMOUNTS = [228, 322]
await economy_queries.credit_awards_batch(session, amounts)
await message.answer(f"🪙 Тугрики розданы {len(winner_ids)} победителям (228 или 322 каждому, итого {total}).")
```
Замечание: повторный `/awards` в тот же день снова раздаёт тугрики — от повторной раздачи защиты НЕТ (известная дыра, в v2 нужен флаг «выплачено за дату»).

---

## 5. Детекция мата — pymorphy3 + словарь лемм (bot/services/profanity_service.py)

Алгоритм (D-03):
```python
def count_profanity(text: str) -> int:
    if not text or _morph is None: return 0
    words = re.findall(r"[а-яёa-z]+", text.lower())   # только буквы, без цифр/пунктуации
    count = 0
    for word in words:
        normal_form = _morph.parse(word)[0].normal_form  # лемматизация pymorphy3
        if normal_form in _profanity_set: count += 1
    return count
```
- Словарь: `data/profanity_ru.txt` — 64 строки, ~60 лемм («блядь», «пизда», «хуй», «еблан», «ебать», «залупа», «мудак», «пиздец», «охуеть», «наебать», «сука», «хуйня», «пиздить»…), пустые строки и `#`-комментарии игнорируются.
- Лемматизация ловит ВСЕ словоформы: «блядям/блядей/блядях» → лемма «блядь» → +1 за каждое вхождение.
- `MorphAnalyzer` грузит ~50 МБ словарь 2–5 сек → инициализация ОДИН раз через явный `profanity_service.init()` из `main()` ПОСЛЕ `setup_logging()` (CR-02), до старта event loop. Module-level singleton `_morph` + `_profanity_set`.
- Библиотеки типа `better-profanity` отвергнуты — плохо работают с русским (CLAUDE.md).

---

## 6. Steam Wishlist + TTL-кэш (bot/services/steam_service.py)

- URL: `https://store.steampowered.com/wishlist/id/mickernon/wishlistdata/` (wishlist конкретного пользователя-админа, D-14).
- In-memory кэш: `_cache: dict | None`, `_cache_time: float` (time.monotonic), `CACHE_TTL = 3600` (1 час, D-16). Кэшируется весь JSON, при каждом вызове — `random.choice(list(data.keys()))` → `{"name": ..., "appid": str}`.
- **Ловушка endpoint'а:** при неавторизованном запросе может отдавать `302 → HTML`. Защита: `allow_redirects=False`, проверка `resp.content_type` содержит `application/json`, `timeout=aiohttp.ClientTimeout(total=5)`.
- Graceful degradation (D-15): при любой ошибке → `None`, карточка показывает fallback-текст; ловятся `aiohttp.ClientError` и `Exception` отдельно, всё логируется warning'ом без падения.
- В карточке ссылка: `<a href="https://store.steampowered.com/app/{appid}/">{html.escape(name)}</a>`.
- В тестах: autouse-фикстура `reset_steam_cache` в conftest.py обнуляет `_cache/_cache_time` до и после каждого теста.

---

## 7. /yuvi — ежедневная лотерея (handlers/yuvi.py, services/yuvi_service.py, db/lottery_queries.py)

- Пул кандидатов = `all_user_ids` из `get_award_winners()` (все, кто писал сегодня).
- `get_or_set_yuvi()` — тот же get-or-set паттерн по `lottery_state(kind='yuvi')`, но `expires_at =` **следующая полночь MSK** (00:00 завтра) — как safety net, если планировщик не сработал. Возвращает `{"user_id", "name", "is_new"}`.
- UX-драматургия: если победитель новый — два сообщения: «🎰 Выбираем Yuvi_Yuvi дня...» → `asyncio.sleep(1)` → карточка. Если уже выбран — одно: «Yuvi_Yuvi сегодня — <b>{name}</b>!». Если никто не писал — «лотерея не состоялась».
- **Сброс в 00:00 МСК** (bot/scheduler.py): `AsyncIOScheduler` + `CronTrigger(hour=0, minute=0, timezone="Europe/Moscow")`, job id=`reset_lottery_statuses`, `replace_existing=True`. Job открывает собственную сессию из `session_factory`, внутри try/except (исключение логируется, планировщик живёт — DoS mitigation T-05-03-01). Сам сброс — один `DELETE FROM lottery_state WHERE kind IN ('yuvi','тугосеря')` + commit.

---

## 8. Статкоманды (handlers/stats.py, services/stats_service.py, db/stats_queries.py)

- **`/stats [@username|reply]`** — карточка: сообщения/слова/фото/форварды (SUM по daily_stats), место в рейтинге, activity-блок. Разрешение цели (`_resolve_stats_target`): приоритет reply → `@username`-аргумент (case-insensitive `func.lower(User.username) == username.lower()`) → сам вызывающий. Ранг: window-функция `func.rank().over(order_by=func.sum(DailyStat.message_count).desc())` по subquery.
- **`/top`** — топ-10: `SELECT first_name, username, SUM(message_count), SUM(word_count) FROM users JOIN daily_stats GROUP BY ... ORDER BY SUM(message_count) DESC LIMIT 10`.
- **`/activity`** (и блок внутри /stats) — распределение по времени суток в МСК прямо в SQL:
  ```python
  moscow_hour = func.extract("hour", func.timezone("Europe/Moscow", Message.sent_at))
  period_expr = case((moscow_hour.between(6, 11), "morning"),
                     (moscow_hour.between(12, 17), "day"),
                     (moscow_hour.between(18, 23), "evening"), else_="night")
  ```
  Рендер — текстовые прогресс-бары `make_progress_bar(value, max)`: `'████░░░░ 50%'` (BAR_LENGTH=8, символы █/░).
- **`/words`** — топ-20 слов: тянет ВСЕ тексты (`get_all_messages_text`, TODO про LIMIT для 100K+), затем `compute_top_words`: `re.findall(r"[а-яёa-z]+", lower)` → фильтр стоп-слов (~50 русских в `STOP_WORDS`) и слов длиной ≤2 → лемматизация pymorphy3 → `Counter.most_common(20)` с `min_count=3`.
- Отправка длинных ответов: `_answer_long` режет по `MAX_MSG_LEN = 4096`.

---

## 9. AI-команды (handlers/ai.py, services/ai_service.py, gemini_service.py, db/ai_queries.py)

- Провайдер: **OpenRouter** через `openai.AsyncOpenAI(base_url="https://openrouter.ai/api/v1", default_headers={"HTTP-Referer": ..., "X-Title": "Yuvi Bot"})`. Исторически Gemini → Groq → OpenRouter; интерфейс `generate(prompt, model_name, system_prompt)` не менялся.
- Модели (`bot/constants/ai.py`, все `:free`): `meta-llama/llama-3.3-70b-instruct:free` (дефолт), `llama-3.1-8b`, `mistral-7b`, `gemma-3-12b`. Модель и системный промпт хранятся в `bot_settings` (ключи `gemini_model`, `system_prompt`) и правятся админ-командами на лету.
- Retry: 2 попытки при `RateLimitError` с паузой 10 сек; на ошибки — человеческие сообщения («⚠️ Превышен лимит…», «Неверный OPENROUTER_API_KEY», «Модель не найдена — /model_list»), никогда traceback.
- Бюджет токенов: `_PER_MSG_LIMIT=150` символов/сообщение, `_TOTAL_CHAR_LIMIT=9000`, `max_tokens=2048`, temperature 0.7. Формат контекста: `[ДД.ММ ЧЧ:ММ] Имя: текст` + маркер обрезки «... (показано X из Y)».
- Команды: `/summary [N]` (посл. N сообщений, деф. 50, макс. 200), `/summary_custom N | фокус`, `/digest [N дней]` с детекцией всплесков активности (`_find_bursts`: часы с >2.5× среднего и ≥5 сообщений — идея для «двойника дня»/дайджестов), `/digest N --debug` (показать промпт без LLM), `/card [@user]` (психопортрет за 14 дней), `/mood`, `/toxic` (оценка 1–10), `/topics` (3–7 тем), `/ask <вопрос>` (RAG по 30 дням).
- Админ (handlers/admin.py): `/prompt_show|set|reset`, `/model_show|list|set`. Проверка прав: `bot.get_chat_member(settings.chat_id, user_id).status in ("administrator","creator")`.

---

## 10. Экономика «тугриков» (Phase 8) — прототип экономики v2

`db/economy_queries.py` (все функции БЕЗ commit — коммитит владелец транзакции):
- Константы: `WELCOME_BONUS=1000`, `TRANSFER_MIN=10`, `TRANSFER_MAX=1_000_000`, `COMMISSION_RATE=20` (т.е. `amount // 20` = 5%), `AWARD_AMOUNTS=[228, 322]` (мемные суммы).
- `get_or_create_wallet` → `(wallet, is_new)`; при создании начисляет welcome-бонус + пишет `Transaction(type="welcome")`. **Race-защита:** INSERT внутри `async with session.begin_nested()` (savepoint); при `IntegrityError` — перечитать чужой кошелёк.
- `transfer(from, to, chat_id, amount)`: комиссия `amount // 20` уходит в `chat_bank`, получатель получает `amount - commission`; проверка `InsufficientFundsError(balance, required)`; две транзакции `transfer_out`/`transfer_in`.
- `credit_awards_batch(session, amounts: dict[user_id, amount])` — массовое начисление победителям /awards с `type="awards"`.
- `get_history` (последние 50), `get_leaderboard` (топ-20 по балансу), `get_economy_stats` (банк, комиссии, число кошельков, суммарная эмиссия = SUM(total_earned)).
- `economy_service.py` — чистые форматтеры (`format_balance`, `format_history`, `format_leaderboard` с маркером «← ты», `format_economy`, `format_transfer_success`). Словарь `_TYPE_LABELS` уже зарезервировал типы будущих фич: `slots_bet, slots_win, market_bet, market_win, market_refund, farm_convert, duel_win, duel_loss, gacha_pull` — то есть рынки/ферма/дуэли/гача планировались на этом же журнале транзакций.
- Handler `/transfer` разрешает получателя через `text_mention` entity (юзер без username) ИЛИ `@username`; запрещает перевод себе.

---

## 11. Казино Mini App (Phase 10)

**Backend:** aiohttp в том же процессе/loop что и polling (`start_api_server` из main.py). `GET /` → redirect `/slot/`; статика webapp/ на `/slot/` с `append_version=True`; POST-эндпоинты `/api/me`, `/api/balance`, `/api/spin`, `/api/history`, `/api/stats`. CORS-middleware опционален (один origin — не нужен).

**Auth (bot/api/auth.py)** — валидация Telegram `initData`, эталонная реализация:
```python
parsed = dict(parse_qsl(init_data, strict_parsing=True))
received_hash = parsed.pop("hash")
# отсечь старые: auth_date старше INIT_DATA_TTL = 24h → None
data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(parsed.items()))
secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
expected = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
hmac.compare_digest(expected, received_hash)  # constant-time
```
Клиент шлёт initData в заголовке `X-Init-Data` при каждом запросе; никаких исключений — при любой ошибке `None` → 401.

**Слот-движок (bot/services/slot_engine.py)** — RNG строго на сервере, клиент только рисует ответ:
- 8 символов с ролями wild/scatter/high/mid/low, весами и выплатами pay3/pay4/pay5 (макс: wild «muscle» 50/200/1000). Каталог обязан 1:1 совпадать с `webapp/slot-data.jsx`.
- 5 барабанов-полос: символы дублируются по весу + детерминированный шафл `j = (i*9301 + (seed+1)*49297) % (i+1)` (LCG, совпадает с JS).
- 10 фиксированных пейлайнов на сетке 3×5; подсчёт слева-направо с учётом wild; scatter в любом месте: 3шт→4 фриспина, 4→6, 5+→7.
- Экономика: `START_BALANCE=2000`, `MIN_BET=10`, `MAX_BET=5000`, `bet_per_line = max(1, bet // 10)`, `BIG_WIN_THRESHOLD_MULT=3` (payout ≥ bet×3 → big_win).
- Anti-spam спинов: in-memory `_LAST_SPIN[user_id]`, минимум 0.25 сек между спинами → 429.
- `apply_spin_result` (db/crud_slot.py): атомарно ставка/фриспин/выигрыш/фриспины-нач./big_win_count + записи в `slot_history`; `bank_total` = SUM(wagered) − SUM(wins).
- `/casino` и алиас `/slot` (handlers/casino.py): в группе — `ReplyKeyboardMarkup` с `KeyboardButton(web_app=WebAppInfo(url))` (inline web_app в группах НЕ работает — важный нюанс), в личке — `InlineKeyboardMarkup`; проверка что URL задан и `https://`.

**Frontend (webapp/):** без сборщика — React 18 UMD + Babel standalone прямо в браузере, `<script type="text/babel">`. Файлы: `telegram.js` (shim SDK), `api.js` (клиент: `window.slotApi.me/balance/spin/history/stats`, база `/api`, override `?api=`), `slot-data.jsx` (зеркало каталога символов), `api-mock.jsx` (dev-мок), `slot-reel/ui/skins/machine.jsx`, экраны `menu/history/stats/rules.jsx`, стили `slot.css` (873 строки) + `webapp.css`. Для v2 (SvelteKit) архитектурно ценны: контракт JSON-ответа `/api/spin` (`grid, wins[], totalPayout, betPlaced, balance, freespinsRemaining, isBigWin, isFreeSpin`) и правило «сервер считает — клиент анимирует».

---

## 12. Тесты (tests/, 155 test-функций в 14 файлах; pytest + pytest-asyncio)

| Файл | Кол-во | Что покрывает |
|---|---|---|
| `test_structure.py` | 58 | «структурные» тесты: существование файлов, AST/regex-проверки исходников (asyncio.run, drop_pending_updates, @dp.error, middleware wiring) — тесты писались ДО кода (TDD по фазам GSD) |
| `test_models.py` | 22 | декларации моделей: tablename, Mapped-стиль, TIMESTAMP(timezone=True), уникальные констрейнты, importability, metadata |
| `test_config.py` | 15 | pydantic Settings: SecretStr-маскирование в repr, fail-fast на отсутствующих env, .gitignore защищает .env |
| `test_awards_queries.py` | 7 | get_award_winners (no data/natural/profanity/spy), get_or_set_tugosera (create/existing/empty) — на AsyncMock-сессии с mock Row |
| `test_collector_filters.py` | 7 | `_is_service_message` по каждому типу сервисного сообщения + 777000 |
| `test_cooldown.py` | 6 | блокировка повтора, разные юзеры/команды независимы, истечение, не-команды проходят, конфигурируемый список |
| `test_yuvi_service.py` | 6 | формат карточки (имя/эмодзи/html-escape/<b>), сценарии нового/существующего победителя |
| `test_profanity.py` | 5 | чистый текст/пусто/мат/регистронезависимость/несколько слов |
| `test_awards.py` | 5 | резолвинг имени пользователя (fallback-цепочка) |
| `test_awards_service.py` | 4 | карточка содержит имя, экранирует HTML, `<b>`-теги, пустой список без данных |
| `test_lottery_queries.py` | 4 | get_or_set_yuvi (none/existing/new), reset делает DELETE |
| `test_stats_service.py` | 10 | прогресс-бары, compute_top_words (стоп-слова/min_count/top_n), форматтеры |
| `test_stats_queries.py` | 3 | case-insensitive поиск username |
| `test_steam_service.py` | 3 | JSON→игра, non-json→None, exception→None (aiohttp мокается) |

Заимствуемые приёмы: (1) `pytest.importorskip("db.awards_queries")` — тесты пишутся до модуля и скипаются пока его нет; (2) `AsyncMock` для AsyncSession + `MagicMock` c `.all()/.scalar_one_or_none()` для Row — юнит-тесты запросов без реального Postgres; (3) autouse-фикстура сброса module-level кэшей (steam); (4) структурные AST-тесты фиксируют архитектурные решения (спорно — хрупкие, в v2 лучше заменить интеграционными на testcontainers/sqlite).

---

## 13. Выводы: что ценно для Yuvi Bot v2 и как интегрировать в экономику эталона (гривны)

**Брать как есть (проверенные паттерны):**
1. Трёхслойный middleware-пайплайн: `dp.update.middleware(DbSession)` → `dp.message.outer_middleware(Collector)` → `dp.message.middleware(Cooldown)` + фильтр chat_id на роутере. Порядок и обоснования — в §2.
2. Инкрементальный `daily_stats` c `ON CONFLICT DO UPDATE` (+ GREATEST для longest) — никаких пересчётов из raw messages; идемпотентный `insert_message` по `message_id`.
3. Детекция мата: pymorphy3-леммы + свой txt-словарь (~60 лемм), init один раз до event loop.
4. Get-or-set суточных статусов через `lottery_state(kind, expires_at)` + APScheduler CronTrigger 00:00 MSK как сброс, `expires_at` как safety net — двойная защита.
5. Валидация initData (auth.py) — переносится в SvelteKit-бекенд v2 практически дословно.
6. «Сервер считает — клиент анимирует» + контракт `/api/spin`; reply-кнопка WebApp для групп.
7. `html.escape` всех имён, HTML parse_mode, `_answer_long` (4096), человеческие тексты ошибок LLM, structlog-события.
8. Savepoint-паттерн `begin_nested()` + IntegrityError для конкурентного создания кошелька.

**Идеи фич → экономика гривны v2:**
- 8 номинаций /awards → ежедневные выплаты в гривнах. Суммы из `AWARD_AMOUNTS`-подобного конфига (мемные 228/322 сохранить). ОБЯЗАТЕЛЬНО добавить защиту от повторной выплаты за день (в старом коде её нет — каждая команда /awards раздаёт заново): таблица `award_payouts(stat_date, user_id, award_key)` или флаг в daily-таблице.
- Автопостинг наград по расписанию (в старом — только по команде; в v2 логично слать в 23:55 MSK schedule-джобой и платить там же).
- «Тугосеря» со статусом на 24ч → в v2 расширить эффектом (дебафф/бафф на заработок гривен, скидка в магазине и т.п.), т.к. инфраструктура статусов (`lottery_state`) уже придумана.
- /yuvi-лотерея → дневной джекпот в гривнах из банка чата (в старом победитель не получал ничего — только титул).
- Банк чата (5% комиссии переводов) → источник призового фонда для лотереи/рынков ставок; формулы `amount // 20` и `get_economy_stats` готовы.
- Журнал `transactions` с `type` — единая шина для всех фич; список типов из `_TYPE_LABELS` (market_bet/market_win/farm_convert/duel_win/gacha_pull...) — фактически готовая номенклатура для v2.
- Слот: перенести движок (символы/пейлайны/скаттер-фриспины/биг-вин ×3) на гривны, ликвидировав отдельный `slot_balance` — единый кошелёк.
- AI-блок: хранение model/prompt в KV `bot_settings` + админ-команды смены на лету; `_find_bursts` — годится для «двойника дня»/дайджестов; бюджетирование контекста (150 симв/сообщение, 9000 всего).

**Известные слабости старого кода (не повторять):**
- Два параллельных кошелька (slot_balance vs wallets) и две генерации кода в одном репо.
- Повторная раздача наград без идемпотентности по дате.
- `/words` грузит все тексты в память без LIMIT.
- Cooldown и rate-limit спинов in-memory — теряются при рестарте (для v2 с Redis — в Redis).
- Steam wishlist endpoint де-факто сломан (302→HTML) — в v2 использовать официальный `IWishlistService/GetWishlist` Web API.
- webapp на Babel-standalone в рантайме — в v2 нормальная сборка SvelteKit.
