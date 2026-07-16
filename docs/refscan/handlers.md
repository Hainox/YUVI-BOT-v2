# Отчёт: bot/handlers/, регистрация роутеров и middleware в эталоне xyloz_tg_bot

Источник: `https://github.com/Heide172/xyloz_tg_bot` (локальная копия: `C:\Users\root\AppData\Local\Temp\claude\C--Users-root-Desktop-YUVI-BOT\327a8fc2-b8d8-4c65-9d50-2d3d3ac0d69d\scratchpad\xyloz_tg_bot`).
Стек: **aiogram >= 3.27, < 4** (long polling), синхронный SQLAlchemy (`SessionLocal`), APScheduler, Python 3.11 (bot.Dockerfile, `PYTHONPATH=/app`, запуск `python bot/main.py`).

---

## 1. Точка входа и регистрация роутеров — `bot/main.py` (95 строк)

### 1.1. Порядок include_router (КРИТИЧЕН)

```python
dp = Dispatcher()                       # без storage, без FSM
dp.include_router(stats_router)         # handlers/statistic.py
dp.include_router(digest_router)        # handlers/digest.py
dp.include_router(user_card_router)     # handlers/user_card.py
dp.include_router(mood_router)          # handlers/mood.py
dp.include_router(topics_router)        # handlers/topics.py
dp.include_router(ask_router)           # handlers/ask.py
dp.include_router(joke_router)          # handlers/joke.py
dp.include_router(casino_router)        # handlers/casino.py
dp.include_router(rules_router)         # handlers/rules.py
dp.include_router(media_dl_router)      # handlers/media_dl.py  <-- ДО message_router!
dp.include_router(admin_status_router)  # handlers/admin_status.py
dp.include_router(feedback_admin_router)# handlers/feedback_admin.py
dp.include_router(farm_admin_router)    # handlers/farm_admin.py
dp.include_router(message_router)       # handlers/messages.py — содержит catch-all в КОНЦЕ
dp.include_router(reaction_router)      # handlers/reactions.py
```

Правило, зафиксированное прямо в докстрингах модулей (`media_dl.py`, `feedback_admin.py`, `farm_admin.py`):
> «Роутер подключать ДО message_router (catch-all в messages.py иначе перехватит)».

`message_router` — предпоследний, потому что в самом низу `messages.py` висит catch-all:

```python
@router.message()
async def message_handler(msg: types.Message):
    save_message(msg)
```

Aiogram останавливает пропагацию после первого сработавшего хендлера, поэтому:
- **сообщения-команды, обработанные роутерами выше, в БД НЕ сохраняются** (catch-all до них не доходит);
- сообщения со ссылками TikTok/Reels/Shorts перехватываются `media_dl` и тоже не попадают в `save_message`;
- «неизвестные» команды (например /balance, /transfer — их хендлеров в боте нет, они живут в Mini App) проваливаются в catch-all и сохраняются как обычные сообщения.

Это осознанный, но грубый вариант «сбора 100%». **В новом проекте лучше сделать outer-middleware** (`dp.message.outer_middleware(...)`) — в эталоне middleware НЕТ вообще (каталог `bot/middlewares/` упомянут в README как план, но в коде отсутствует; grep по `middleware` в bot/ пуст).

### 1.2. Меню команд по scope

Два списка `BotCommand` и двойной `set_my_commands` — паттерн стоит забрать:

```python
await bot.set_my_commands(PUBLIC_COMMANDS, scope=BotCommandScopeDefault())
await bot.set_my_commands(ADMIN_COMMANDS, scope=BotCommandScopeAllChatAdministrators())
```

`ADMIN_COMMANDS = PUBLIC_COMMANDS + [...]` — админ видит и публичные, и технические.

### 1.3. Запуск

```python
await setup_commands(bot)
scheduler = start_scheduler(bot)   # APScheduler (AsyncIOScheduler, TZ Europe/Moscow)
try:
    await dp.start_polling(bot)
finally:
    scheduler.shutdown(wait=False)
```

`start_polling` без явных `allowed_updates` — aiogram сам собирает типы апдейтов из зарегистрированных хендлеров (message + chat_member; см. баг с реакциями в §3.14).

---

## 2. Middleware, фильтры, DI-сессии — чего НЕТ и что вместо этого

- **Middleware отсутствуют полностью.** Ни anti-flood/cooldown, ни логирующего, ни инжекции сессии.
- **AsyncSession не используется.** Везде синхронный SQLAlchemy: `common/db/db.py` → `engine = create_engine(DATABASE_URL, pool_pre_ping=True, pool_recycle=1800, pool_size=DB_POOL_SIZE(20), max_overflow=DB_MAX_OVERFLOW(30), pool_timeout=DB_POOL_TIMEOUT(10), connect_args={keepalives...})`, `SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)`.
- Каждый хендлер/сервис сам делает `session = SessionLocal(); try: ... finally: session.close()`. Тяжёлые синхронные вызовы оборачиваются `await asyncio.to_thread(fn, ...)` (media_dl, feedback_admin, farm_admin), но многие статистические хендлеры (`statistic.py`) блокируют event loop прямыми запросами — известная слабость эталона.
- **Фильтры**: только `Command(...)` из `aiogram.filters` и один магический фильтр `F.text.regexp(...)` в media_dl. Кастомных классов-фильтров нет.
- **Права**: два независимых механизма:
  1. «Админ бота» — `bot/services/admin_service.py`: `BOT_ADMIN_IDS` (env, CSV из tg_id) → `is_admin_tg_id(tg_id)`. Проверка внутри хендлера: `def _require_admin(msg): return bool(msg.from_user and is_admin_tg_id(msg.from_user.id))`, при отказе — `msg.answer("Только для админов бота.")`. Нет декоратора/фильтра — копипаста в каждом admin-хендлере.
  2. «Админ чата» — `statistic.py::_is_admin(bot, chat_id, user_id)` через `bot.get_chat_member(...).status in ("administrator", "creator")` — используется только для флага `--chat <id>` (просмотр чужого чата).
- **FSM не используется** (диалог создания рынка переехал в Mini App).
- **Callback-хендлеров НЕТ вообще** (grep по `callback_query|CallbackQuery|F.data` — 0 совпадений). Вся интерактивность — Mini App; единственная inline-кнопка — URL-кнопка deep-link в /casino.

---

## 3. Handler-модули (все 15)

### 3.1. `messages.py` (398 строк) — summary, промпт/модель, /fag, catch-all
Роутер регистрируется предпоследним.

| Команда | Права | Что делает |
|---|---|---|
| `/prompt_show`, `/prompt_set <text>`, `/prompt_reset` | админ бота | системный промпт пересказа (хранится в БД через summary_service) |
| `/model_show`, `/model_list`, `/model_set <model>` | админ бота | текущая AI-модель (env `AI_AVAILABLE_MODELS`, `SUMMARY_MODEL`) |
| `/summary [N]`, алиас `/sum` | все | стриминговый пересказ N последних сообщений |
| `/summary_custom N \| промпт`, алиас `/sumc` | все | пересказ с кастомной задачей; парсер `_parse_custom_summary_args` |
| `/fag` | все | «пидор дня»: детерминированный выбор на день (MSK) из вчерашних активных, бонус в гривнах (идемпотентно, `award_fag`), авто-тег через `assign_nomination_tag` |
| `@router.message()` catch-all | — | `save_message(msg)` — сбор всех остальных сообщений |

Ключевые паттерны для заимствования:

**(а) Стриминг LLM в Telegram через send_message_draft с фолбэком на edit-throttling** (aiogram 3.27+, Bot API drafts):
```python
draft_id = msg.message_id  # уникальный non-zero per chat
try:
    await msg.bot.send_message_draft(chat_id=chat_id, draft_id=draft_id,
                                     text="Собираю контекст...", message_thread_id=thread_id)
except TelegramBadRequest as exc:
    if not _is_draft_unsupported(exc):  # "TEXTDRAFT" in str(exc).upper()
        raise
    use_drafts = False
    progress = await msg.answer(initial_text)   # обычное сообщение + edit_text
interval = 1.0 if use_drafts else 2.5           # частота обновлений
```

**(б) Мост sync-LLM-стрим → async-UI через `queue.Queue` + фоновая корутина-updater** (один и тот же блок скопирован в ask.py/digest.py/user_card.py):
```python
content_q: Queue[str] = Queue(); reasoning_q: Queue[str] = Queue(); done = False
async def updater():
    while not done or not content_q.empty() or not reasoning_q.empty():
        ... # выгребаем get_nowait(), редактируем сообщение не чаще interval
        await asyncio.sleep(interval)
updater_task = asyncio.create_task(updater())
summary_text = await asyncio.to_thread(stream_summary, prompt, content_q.put, reasoning_q.put)
done = True; await updater_task
```
Пока нет content — показывается «превью рассуждений» (`_format_reasoning_preview`, хвост 400 символов reasoning-стрима).

**(в) Защита от Telegram-ошибок**: `_safe_edit_text` глотает `message is not modified`; `TelegramRetryAfter` → `await asyncio.sleep(exc.retry_after)`; `_split_text_chunks(text, 3900)` режет длинные ответы по `\n` (первый чанк — edit, остальные — `msg.answer`).

**(г) Thread-логика**: везде при отправке в группу с топиками передаётся `message_thread_id=msg.message_thread_id` (drafts, send_message, send_document).

### 3.2. `statistic.py` (575 строк) — /mystats, /chatstats, /who, /peakday, /streak, /help
Все — публичные. Общие утилиты:
- `_parse_args(text)` → `(days 1..365, default 14; chat_id из "--chat <id>")`;
- `_check_chat_access` — `--chat` чужого чата только админам чата;
- `_send_mono(message)` — вывод в `<pre>` + `html.escape` (`parse_mode="HTML"`) — моноспейс-таблицы без ломания разметки матом/спецсимволами;
- `_top_words` — частотка по regex `[A-Za-zА-Яа-яЁё][A-Za-zА-Яа-яЁё\-]{3,}` с большим русским STOPWORDS-сетом (~120 слов, стоит скопировать целиком);
- `_peak_hour` — `func.extract("hour", Message.created_at)` group by.

Запросы: `func.count`, `func.date_trunc("day", ...)`, `reply_to.in_(subquery)` для «сколько раз тебе ответили», ранжирование в Python. `/help` — статический `<pre>`-текст HELP_TEXT со всеми командами.

Замечание: сессии открываются в начале хендлера и держатся до конца (включая await'ы) — в новом проекте лучше выделять запросы в сервис + to_thread.

### 3.3. `digest.py` (181) — `/digest [N] [--debug]` (все)
- `--debug`: собирает промпт без вызова LLM и шлёт его файлом: `BufferedInputFile(prompt.encode(), filename=f"digest_debug_{ts}.txt")` + caption с байтами/~токенами. Отличный паттерн отладки промптов.
- Обычный режим: тот же стриминг-паттерн (progress-сообщение + updater + чанкование).

### 3.4. `user_card.py` (152) — `/card [@user]` (все)
Резолв цели в приоритете: явный аргумент `@username` → reply на сообщение (`msg.reply_to_message.from_user.id`) → сам автор. `resolve_user_for_card(chat_id, arg_text, fallback_tg_id, reply_to_tg_id)`. Далее — стриминг-паттерн.

### 3.5. `mood.py` (45) — `/mood [N]`, `/toxic [N]` (все)
Простые: `parse_days` → `build_mood_report` / `build_toxic_report` (по NLP-колонкам sentiment/toxicity из таблицы messages) → `msg.answer(text)`.

### 3.6. `topics.py` (83) — `/topics [N]` (все)
`discover_topics` (эмбеддинги + кластеризация). Возвращает `None` = мало данных (< 30 сообщений от 20 симв.), `[]` = кластеры не нашлись — оба случая с отдельными текстами.

### 3.7. `ask.py` (135) — `/ask <вопрос>` (все) — RAG по истории чата
`parse_ask_query` → `stream_ask(chat_id, query, on_delta, on_reasoning)` → тот же updater-паттерн (интервал 3.0 сек, обычный edit, без drafts).

### 3.8. `joke.py` (82) — `/joke`, `/phrase` (все; флаг `--new` только админ бота)
Паттерн «тикер прогресса»: `_tick` каждые 15 сек редактирует «Думаю над анекдотом… (30 сек)», обёрнут в `_run_with_progress` c `asyncio.wait_for(coro, timeout=300)`; при таймауте — понятное сообщение. `force=--new` — сброс кэша «дня» (только админ).

### 3.9. `casino.py` (44) — `/casino` (все) — вход в Mini App
Важный обход: **WebAppInfo-кнопка в группах не работает (BUTTON_TYPE_INVALID)**, поэтому deep-link:
```python
f"https://t.me/{me.username}?startapp={chat_id}"   # chat_id приедет в initData.start_param
```
Отдаётся обычной URL-кнопкой `InlineKeyboardButton(text="Открыть казино", url=link)`. Переопределение базовой ссылки — env `MINIAPP_DEEPLINK`.

### 3.10. `rules.py` (56) — `/rules` (все)
Статический `RULES_TEXT` (экономика «Бурмалда», parimutuel-рынки, комиссии) в `<pre>`.

### 3.11. `media_dl.py` (148) — авто-скачивание TikTok/Reels/Shorts (все, без команды)
Единственный не-Command фильтр:
```python
_URL_FILTER = r"https?://\S*(?:tiktok\.com|instagram\.com|youtube\.com/shorts|youtu\.be)\S*"
@router.message(F.text.regexp(_URL_FILTER))
```
Логика (платная операция, cobalt self-host, env `COBALT_API_URL`, `MEDIADL_COST=50`, `MEDIADL_MAX_MB=48`):
1. `msg.reply("Скачиваю видео… (−50г)")` — progress;
2. списание вперёд `charge(user_id, chat_id)` (в банк чата, sink), при `InsufficientFunds` — отказ;
3. `download_sync(url)` в `asyncio.to_thread`; при ошибке — `refund` + edit_text;
4. отправка: 1 файл → `send_photo/send_video(FSInputFile(path))`, несколько → `send_media_group` с `InputMediaPhoto/InputMediaVideo` (caption только у первого);
5. reply/удаление: если в сообщении только ссылка (`rest < 3` симв. после вырезания URL) — исходник удаляется, ссылка дублируется в caption бота; если есть авторский текст — исходник не трогаем, медиа шлётся `reply_to_message_id=msg.message_id`;
6. caption: `"📥 от @user · −50г" [+ url] [+ описание поста]`;
7. при ошибке отправки (>50МБ) — refund; `finally: os.remove(path)`.
`_db_user_id` — get-or-create User по tg_id (локальная копия, та же логика что в save_message).

### 3.12. `admin_status.py` (252) — `/admin_status`, `/backfill` (админ бота)
- `/admin_status`: `gather_status()` → форматирование в `<pre>`: версия (env `BUILD_SHA`/`BUILD_TIME`), uptime, health сервисов с latency, покрытие данных по чатам (emb/nlp done + gap), задачи APScheduler (`next_run`), backfill jobs, размеры таблиц, perf API из Redis.
- `/backfill list|start <embed|nlp>|stop <name>`: управление in-process backfill-воркерами (`bot/services/backfill_runner.py`): `JOB_REGISTRY = {"embed": embed_pending_once, "nlp": classify_pending_once}`; `start_job` создаёт `asyncio.create_task(_run_loop(job))`; цикл зовёт `fn()` пока возвращает >0, после 3 холостых итераций (по 5 сек) останавливается; учёт `processed`, `rate_per_sec`, `last_error`. После старта хендлер запускает `_monitor_job_progress` — фоновая корутина каждые 15 сек редактирует прогресс-сообщение в чате до завершения. **Это backfill производных данных (эмбеддинги/NLP), не сообщений.**

### 3.13. `feedback_admin.py` (135) — `/fb list|show <id>|done <id> [сумма]` (админ бота)
Модерация фидбэка из Mini App: закрытие заявки начисляет автору награду (эмиссия) в том чате, откуда прислан фидбэк (`REWARD_BUG`/`REWARD_IDEA`, env `FEEDBACK_REWARD_BUG/IDEA`; сумма 0 = без награды). После закрытия — best-effort уведомление автора в ЛС через `social_service.send_chat_message` (в `asyncio.to_thread`). Все sync-вызовы сервиса через `asyncio.to_thread`.

### 3.14. `reactions.py` (9 строк) — сбор реакций. ВНИМАНИЕ, СЛОМАН
```python
@router.chat_member()   # или событийный хендлер для реакций
async def reaction_handler(event: MessageReactionUpdated):
    save_reaction(event)
```
Две ошибки, которые НЕ надо повторять:
1. Хендлер повешен на `chat_member`-апдейты, а тип аргумента — `MessageReactionUpdated`. Правильно: `@router.message_reaction()` + в polling должен попасть `allowed_updates=["message_reaction", ...]` (реакции по умолчанию не приходят).
2. `save_reaction` пишет `emoji=event.new_reaction` — а это `list[ReactionType]`, не строка; и `message_id=event.message_id` кладётся в FK на `messages.id` (внутренний PK), хотя это telegram message_id. В новой схеме реакции надо связывать по `(chat_id, telegram_message_id)` и сериализовать список реакций (added/removed diff old_reaction/new_reaction).

### 3.15. `farm_admin.py` (64) — `/farmwipe here|<chat_id>|all CONFIRM` (админ бота)
Паттерн деструктивной команды: обязательное третье слово `CONFIRM`, иначе HELP. `wipe_farm_sync` через to_thread, отчёт по числу удалённых строк (farms/gacha/pool/price).

---

## 4. Сбор 100% сообщений — как реально устроено

### 4.1. Live-приём: `bot/services/message_service.py::save_message(tg_message)`
```python
user = session.query(User).filter_by(tg_id=...).first() or User(...); session.flush()
msg = Message(
    message_id=tg_message.message_id,
    telegram_message_id=tg_message.message_id,   # дубль для совместимости с history-импортом
    chat_id=tg_message.chat.id,
    user_id=user.id,
    text=tg_message.text,
    emojis="".join(e for e in text if emoji.is_emoji(e)),   # либа `emoji`
    sticker=tg_message.sticker.file_id if ... else None,
    media=tg_message.photo[-1].file_id if tg_message.photo else None,  # максимальный размер фото
    reply_to=tg_message.reply_to_message.message_id if ... else None, # telegram message_id, НЕ PK
    created_at=tg_message.date, edited_at=tg_message.edit_date,
)
```
Ошибки: `rollback()` + лог, бот не падает. Замечания: caption/video/voice/document в live-пути НЕ сохраняются (только photo/sticker); `edited_message` апдейты не обрабатываются вовсе; `message_thread_id` не сохраняется в БД. В новом проекте — расширить.

### 4.2. Модель `common/models/message.py` (таблица `messages`)
Колонки: `id BIGINT PK`, `message_id`, `telegram_message_id` (уникальный индекс `idx_chat_telegram_message (chat_id, telegram_message_id)` — основа идемпотентности backfill), `user_id FK users.id (nullable)`, `chat_id`, `text`, `emojis`, `sticker`, `media`, `reply_to`, `message_type ('text'|'photo'|...)`, медиа-блок: `file_id, file_unique_id, file_name, mime_type, file_size, caption, has_media, is_forwarded, forward_from`, времена `created_at/edited_at`, NLP-блок: `sentiment_score/-label, toxicity_score, topic_id, nlp_processed_at`. Индексы: `idx_chat_message`, `idx_user_chat`, `idx_created_at`, `idx_nlp_unprocessed`, `idx_chat_sentiment`.

### 4.3. Backfill исторических сообщений: `scripts/history_load.py` (693 строки, интерактивный CLI)
- **Pyrogram (user-аккаунт)**: `Client(TG_SESSION_NAME, api_id=TG_API_ID, api_hash=TG_API_HASH, phone_number=TG_PHONE)` — env `TG_API_ID`, `TG_API_HASH`, `TG_PHONE`, `TG_SESSION_NAME`.
- `resolve_peer`: поиск чата по диалогам → `get_chat` → перебор вариантов ID супергрупп (`-100...`-префикс).
- Итерация `client.get_chat_history(chat_id, limit)`, tqdm-прогресс, **коммит батчами BATCH_SIZE=100**, rollback на ошибке отдельного сообщения.
- Идемпотентность: скип если существует `(telegram_message_id, chat_id)`.
- `get_message_type` — 12 типов (photo/video/audio/voice/document/sticker/animation/video_note/location/contact/poll/text/other); `extract_media_info` вытаскивает `file_id/file_unique_id/file_name/mime_type/file_size` для каждого типа. `text = message.text or message.caption`; форварды (`forward_from`/`forward_from_chat` id как строка), reply_to, emojis — как в live.
- ВАЖНО: file_id, полученный юзер-клиентом Pyrogram, **не воспроизводим Bot API** — в эталоне это просто архивные метаданные.

### 4.4. Догон производных данных
Постоянно: APScheduler джобы `nlp_classify_pending` (каждые 30 сек) и `embed_pending` (каждые 45 сек); форсированно: `/backfill start embed|nlp` (см. §3.12). Плюс standalone-скрипты `scripts/embed_backfill.py`, `scripts/nlp_backfill.py`.

---

## 5. Планировщик (регистрируется в main, `bot/services/scheduler.py`)
`AsyncIOScheduler(timezone=Europe/Moscow)`; jobs (все `coalesce=True, max_instances=1`): `weekly_digest` (пн 9:00 МСК, рассылка по `find_active_chat_ids(window_days=14)`), `nlp_classify_pending` (30 c), `embed_pending` (45 c), `daily_nominations` (10:00 МСК, с рассылкой в чаты), `external_markets_check` (30 мин), авто-закрытие рынков, экспирация аренды тегов и номинационных тегов. Модульная ссылка `_scheduler` + `get_scheduler()` — чтобы `/admin_status` показывал `next_run` джобов.

---

## 6. Реакции бота на пользователей / теги
- **Реакций-эмодзи бот не ставит** (`set_message_reaction` не используется).
- «Тег» = `custom_title` админа: `bot/services/tag_service.py` — Telegram позволяет подпись только админам и максимум **16 символов** (`TITLE_MAX = 16`), поэтому паттерн: `promote_chat_member` с безобидным правом → `set_chat_administrator_custom_title`; снятие = promote со всеми правами False. Держатель хранится в таблице `bot_settings` как `nomtag:<...>` → `tg_id:YYYY-MM-DD`. Используется для «пидора дня» (`assign_nomination_tag(bot, chat_id, tg_id, "пидор дня", "fag")`) и рынка аренды тегов.

---

## 7. Env-переменные, задействованные в bot/
`TELEGRAM_TOKEN`, `BOT_ADMIN_IDS` (CSV tg_id), `DATABASE_URL`, `DB_POOL_SIZE/MAX_OVERFLOW/TIMEOUT`, `MINIAPP_DEEPLINK`, `COBALT_API_URL`, `MEDIADL_COST/MAX_MB/CAPTION_MAX`, `AI_AVAILABLE_MODELS`, `SUMMARY_MODEL`, `OPENCODE_API_KEY/BASE_URL`, `NLP_SERVICE_URL`, `BUILD_SHA/BUILD_TIME`, `FEEDBACK_REWARD_BUG/IDEA`, `ECONOMY_START_BONUS`, `TRANSFER_FEE_*`, `MARKET_*`, `CASINO_MIN/MAX_BET`, `CLICKER_*` (ферма), `GACHA_*`, `DUEL_*`, `TAG_RENT_PER_DAY`, `NOMINATION_*`, `ASK_*`, `EMBED_*`, `NLP_*`; для history_load: `TG_API_ID/TG_API_HASH/TG_PHONE/TG_SESSION_NAME`.

---

## 8. Что брать в Yuvi Bot v2 и что исправить

**Брать:**
1. Двухуровневое меню команд по scope (Default / AllChatAdministrators).
2. Порядок роутеров: специализированные → фильтр-роутеры (media_dl) → catch-all последним; комментарии о порядке прямо в докстрингах модулей.
3. Стриминг LLM: drafts (`send_message_draft`, draft_id=msg.message_id) с фолбэком на edit-throttling; Queue+updater; `_safe_edit` («message is not modified»); `TelegramRetryAfter.retry_after`; чанкование 3900 по `\n`; reasoning-preview.
4. `<pre>`+`html.escape` для табличного вывода; `--debug` промпта файлом (`BufferedInputFile`).
5. Идемпотентный backfill Pyrogram'ом с уникальным индексом `(chat_id, telegram_message_id)` и батч-коммитами.
6. Deep-link `?startapp=<chat_id>` вместо WebApp-кнопки в группах.
7. `CONFIRM`-паттерн деструктивных команд; `/backfill`-раннер с мониторингом прогресса в чате; тикер прогресса + `asyncio.wait_for` таймаут для долгих LLM-команд.
8. Custom_title-теги (promote + set_chat_administrator_custom_title, лимит 16 симв.).

**Исправить/сделать иначе:**
1. Сбор 100% — через `outer_middleware` на `dp.message` (и `edited_message`, `message_reaction`), а не catch-all: тогда сохраняются И команды, И перехваченные media_dl ссылки.
2. Реакции: `@router.message_reaction()` + явный `allowed_updates`; правильная сериализация `new_reaction/old_reaction`; связь по `(chat_id, telegram_message_id)`.
3. Async SQLAlchemy + DI-middleware сессии вместо ручного `SessionLocal()` в каждом хендлере; убрать блокирующие запросы из event loop.
4. Cooldown/анти-спам middleware (в эталоне отсутствует).
5. Фильтр/декоратор прав админа вместо копипасты `_require_admin`.
6. Сохранять `message_thread_id`, caption-медиа всех типов, edited_message.
