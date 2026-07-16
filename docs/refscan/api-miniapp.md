# Отчёт: api/ (FastAPI) и miniapp/ (SvelteKit) в эталоне xyloz_tg_bot

Источник анализа: локальный клон `xyloz_tg_bot` (папки `api/`, `miniapp/`, плюс связанные `bot/handlers/casino.py`, `common/events.py`, `docker-compose.yml`).

---

## 1. Общая архитектура связки

```
Telegram-клиент
   └─ Mini App (SvelteKit SPA, adapter-static, раздаётся nginx, порт 8003)
        └─ fetch → FastAPI (api/, uvicorn ×3 воркера, порт 8002, префикс /api/v1)
             ├─ переиспользует bot/services/* и common/models/* через PYTHONPATH=/app:/app/bot
             ├─ PostgreSQL (SQLAlchemy, SessionLocal)
             └─ Redis pub/sub → SSE-пуш баланса обратно в Mini App
```

Ключевое решение: **API — тонкий слой над сервисами бота**. Вся бизнес-логика (казино, ферма, рынки, дуэли, гача) живёт в `bot/services/*_service.py` и вызывается и из aiogram-хендлеров, и из FastAPI-роутов. Sync-функции сервисов зовутся из async-роутов через `asyncio.to_thread(...)` — это главный паттерн всего `api/`.

Файлы:
- `api/main.py` — приложение, CORS, middleware метрик, подключение роутеров
- `api/auth.py` — валидация Telegram initData + членство в чате + админ-проверка
- `api/schemas.py` — общие Pydantic-схемы (часть схем объявлена локально в роутерах)
- `api/serializers.py` — ORM → Pydantic (`user_to_schema`, `market_to_schema`, `portfolio_item`)
- `api/routes/{economy,markets,portfolio,games,admin,clicker,history,stats,social,duel,tags,gacha,feedback,events,analytics}.py`
- `api/Dockerfile`, `api/requirements.txt`

`api/requirements.txt`: fastapi>=0.110, uvicorn[standard], pydantic>=2.5, SQLAlchemy>=2, psycopg2-binary, python-dotenv, pgvector, aiohttp, openai>=1.40, redis>=5.

`api/Dockerfile`: python:3.12-slim, `ENV PYTHONPATH=/app:/app/bot`, запуск:
```
CMD ["sh", "-c", "uvicorn api.main:app --host 0.0.0.0 --port 8000 --workers ${API_WORKERS:-3}"]
```

### api/main.py — каркас
- `FastAPI(title="xyloz-bot-api", version="0.1.0")`
- CORS: `API_CORS_ORIGINS` (env, csv, дефолт `*`), `allow_credentials=False`, все методы/заголовки.
- HTTP-middleware `_perf_mw`: замеряет длительность каждого запроса, пишет в `common.metrics.record_request(route, method, status, dur_ms)`; раз в 2с на воркер — снапшот пула SQLAlchemy (`engine.pool.size/checkedout/overflow`) через `record_pool`. Всё в try/except — метрики не могут уронить запрос.
- Публичные эндпоинты без auth: `GET /` (инфо), `GET /health`, `GET /api/v1/ping` (лёгкий пинг — Mini App детектит «сервис ожил» после редеплоя).
- Роутеры и префиксы:

| Router | Prefix |
|---|---|
| economy | `/api/v1` |
| markets | `/api/v1/markets` |
| portfolio | `/api/v1/portfolio` |
| games | `/api/v1/games` |
| admin | `/api/v1/admin` |
| clicker | `/api/v1/farm` |
| history | `/api/v1/history` |
| stats | `/api/v1/stats` |
| social | `/api/v1/social` |
| duel | `/api/v1/duel` |
| tags | `/api/v1/tags` |
| gacha | `/api/v1/gacha` |
| feedback | `/api/v1/feedback` |
| events (SSE) | `/api/v1` |
| analytics | `/api/v1` |

---

## 2. Авторизация Mini App (api/auth.py) — заимствовать целиком

### 2.1 HMAC-валидация initData
По официальной схеме Telegram (`https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app`):

```python
def _verify_init_data(init_data: str) -> dict:
    pairs = dict(parse_qsl(init_data, keep_blank_values=True))
    sent_hash = pairs.pop("hash", None)                      # 401 если нет
    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret_key = hmac.new(b"WebAppData", _bot_token().encode(), hashlib.sha256).digest()
    calc_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calc_hash, sent_hash):        # constant-time!
        raise HTTPException(401, "initData: hash mismatch")
    if auth_date and time.time() - auth_date > INIT_DATA_MAX_AGE_SEC:  # env TMA_INIT_DATA_MAX_AGE, дефолт 86400 (24ч)
        raise HTTPException(401, "initData: too old")
```

Токен — env `TELEGRAM_TOKEN` (тот же, что у бота).

### 2.2 Зависимость require_auth
```python
async def require_auth(request, x_telegram_init_data: str | None = Header(alias="X-Telegram-Init-Data"),
                       chat_id: int | None = Query(default=None)) -> TgWebAppAuth
```
- initData берётся из заголовка `X-Telegram-Init-Data` **или** из query `init_data` (нужно для SSE — EventSource не умеет заголовки).
- Из `pairs["user"]` (JSON) собирается `TgWebAppUser(id, username, first_name, last_name, language_code)`.
- Возвращает dataclass `TgWebAppAuth(user, auth_date, chat_id)` — `chat_id` приходит **query-параметром** от фронта (Mini App одна на много чатов; чат передаётся при каждом запросе).

### 2.3 ensure_db_user(auth) → int
Get-or-create строки в таблице `users` по `tg_id` (колонки `tg_id`, `username`, `fullname`); возвращает внутренний `user.id`. Вызывается почти в каждом роуте.

### 2.4 Членство в чате — require_chat_membership(auth) → chat_id
- `require_chat_id`: 400, если query `chat_id` не передан.
- `_is_member_sync`: прямой HTTP GET к Bot API `https://api.telegram.org/bot<token>/getChatMember?chat_id=..&user_id=..` (stdlib urllib, timeout 5с); True при статусе `creator|administrator|member|restricted`.
- `is_chat_member`: in-memory кэш `dict[(chat_id, user_tg_id)] -> (bool, ts)` с TTL `TMA_MEMBERSHIP_CACHE_TTL` (дефолт 300с) под `asyncio.Lock`, сам запрос через `asyncio.to_thread`. Это и авторизация, и де-факто защита Bot API от rate limit.
- 403 «Ты не состоишь в этом чате.» если не участник.

### 2.5 Админ
`is_admin(tg_id)` — членство в env-списке `BOT_ADMIN_IDS` (csv tg_id). В admin-роутере — локальный helper `_ensure_admin(auth)` → 403.

### 2.6 Rate limiting
**Выделенного rate-limit middleware в API нет** (нет slowapi и т.п.). Анти-абьюз точечный, на уровне доменной логики:
- ферма: серверный кэп тапов `MAX_CPS` (env `CLICKER_MAX_CPS`, дефолт 30): `accepted = min(count, MAX_CPS * elapsed_ms / 1000)` в `clicker_service.py` (клиент дополнительно троттлит до 20 тапов/с);
- слоты: идемпотентность через `idem_key` (unique-колонка в `casino_games`) — повтор запроса после сбоя возвращает тот же исход без второго списания;
- кэш членства (300с) ограничивает обращения к Bot API;
- валидация Pydantic `Field(ge=..., le=...)` на всех суммах/лимитах.
Для нового проекта: закладывать отдельный rate limit (хотя для «чата друзей» эталон обошёлся без него).

---

## 3. Полный список роутов API

Все роуты, кроме `/`, `/health`, `/api/v1/ping`, требуют `Depends(require_auth)`; пометка **[member]** = дополнительно `require_chat_membership`, **[admin]** = проверка `BOT_ADMIN_IDS`. Ошибки доменных сервисов маппятся в HTTP через локальные `_map_error/_err`: InvalidArgument→400, InsufficientFunds→400 (в markets — 402), NotFound→404, MarketClosed/GameNotActive/уже-закрыто→409, прочее→500.

### 3.1 economy (`/api/v1`) — api/routes/economy.py
| Метод | Путь | Назначение | Запрос → Ответ |
|---|---|---|---|
| GET | `/me` | профиль + баланс + is_admin | → `MeResponse{user: UserPublic, balance: BalanceResponse\|null, is_admin}`. Мягкий режим: если не член чата — просто `balance=null`, без 403 |
| GET | `/balance` **[member]** | баланс юзера + банк чата | → `BalanceResponse{chat_id, balance, bank}` |
| GET | `/leaderboard?limit=20` **[member]** | топ по балансу | → `{entries: [{user, balance}]}` |
| GET | `/transactions?limit=50` **[member]** | мои транзакции (таблица `economy_tx`) | → `{items: [TxItem{id, amount, kind, note, created_at}]}` |
| GET | `/members?q=&limit=10` **[member]** | автокомплит участников (join `users`+`user_balance` по chat_id, ilike по username/fullname, сортировка по балансу) | → `{items: [{tg_id, username, fullname}]}` |
| GET | `/transfer/quote?amount=N` **[member]** | предрасчёт комиссии | → `{amount, fee, total}` |
| POST | `/transfer` **[member]** | перевод с комиссией 5% (мин 1) | `{target: "@username\|tg_id", amount>=1, note?}` → `{amount, fee, total, sender_balance, receiver_balance, receiver_username}` |

Паттерн резолва получателя `_resolve_user(target)`: regex `^@?([A-Za-z0-9_]{3,32})$` → поиск по username, иначе парс как `tg_id` int (повторён в admin.py и duel.py).

### 3.2 markets (`/api/v1/markets`) — рынки ставок (parimutuel)
| Метод | Путь | Назначение |
|---|---|---|
| GET | `` `?status=open\|closed\|resolved\|cancelled\|all` **[member]** | список рынков чата (limit 50) → `MarketsList{items: [MarketPublic]}` |
| GET | `/{market_id}` **[member]** | детали → `MarketPublic` (404, если рынок не этого чата — проверка `view.market.chat_id != chat_id`) |
| POST | `` **[member]** | создать: `CreateMarketRequest{question, options: [str], duration: "7d/12h/90m"}` → `{market, fee_charged}`; InsufficientFunds → **402** |
| POST | `/{market_id}/bets` **[member]** | ставка: `{option_position, amount}` → `{bet_id, market_id, option_label, option_pool_after, user_balance_after}`; MarketClosed → 409 |
| POST | `/import` **[member]** | импорт внешнего рынка (Polymarket/Manifold) по URL: `{url}` → `{market, already_imported}` |

`MarketPublic`: `{id, chat_id, type(internal|polymarket|manifold), status, question, options: [{id, position, pool, label, share(0..1)}], total_pool, bets_count, closes_at, resolved_at, winning_option_id, external_url, creator_id, created_at}`. `share` считается в сериализаторе: `o.pool / total`.

### 3.3 portfolio (`/api/v1/portfolio`)
| GET | `` **[member]** | открытые позиции юзера → `{items: [PortfolioBet{bet_id, market_id, question, status, option_label, amount, payout, refunded, created_at}]}` |

### 3.4 games (`/api/v1/games`) — казино
Все **[member]**; единый ответ `GameResp{game_id, game, outcome, bet, payout, net, user_balance_after, bank_after, details: dict}` (маппится из dataclass `GameResult` сервиса `casino_service`):
| POST | `/coinflip` | `{bet>=1, pick: "heads"\|"tails"}` |
| POST | `/dice` | `{bet, mode: "over"\|"under", threshold: 1..99}` |
| POST | `/slots` | `{bet, idem_key?: str<=40}` — details содержит `grid` 5x3, `win_lines`, `freespins[]`, `scatter_payout` |
| POST | `/roulette` | `{bet, bet_type: "number"\|"color"\|"parity"\|"half"\|"dozen", value?}` |
| POST | `/blackjack/start` | `{bet}` → outcome может быть `active` |
| POST | `/blackjack/{game_id}/hit` · `/stand` · `/double` | шаги партии; GameNotFound→404, GameNotActive→409 |

Экономика игр — double-entry против банка чата (`chat_bank`), поэтому в ответе всегда `bank_after`.

### 3.5 clicker/ферма (`/api/v1/farm`)
Все **[member]**; единый ответ `FarmStateResp{cp_balance, tap_level, auto_level, auto_rate_cps, next_tap_cost, next_auto_cost, bank_balance, user_balance, lifetime_cp, cp_per_hryvnia, offline_cap_seconds, workers[]}`:
| GET | `` | состояние |
| POST | `/tap` | `{count: 1..5000, elapsed_ms>=1}` — батч тапов; elapsed без потолка (сворачивание вебвью замораживает таймеры), сервер кэпит по MAX_CPS |
| POST | `/upgrade/tap` · `/upgrade/auto` | апгрейды |
| POST | `/hire/{wtype}` | нанять работницу (cherry/lemon/bell/star/diamond) |
| POST | `/convert` | `{cp_amount}` — продать cp за гривны через AMM (constant-product, slippage) |
| POST | `/buy` | `{hryvnia_amount}` — обратный поток, двигает курс вверх |
| GET | `/market` | котировка AMM + `history` (200 точек) для графика |

### 3.6 history (`/api/v1/history`) — лента прозрачности
| GET | `?limit=50&offset=0` **[member]** | все денежные события чата → `{items: [HistoryItem{id, created_at, user_id, username, fullname, amount, kind, note}], has_more}` |

Паттерны: `has_more` через выборку `limit+1`; фильтр `HIDDEN_KINDS` — служебные double-entry зеркала банка (`casino_*_bet_to_bank`, `casino_*_payout_from_bank`, `market_create_fee_bank`, `clicker_convert_from_bank`, …) не показываются, т.к. у них есть user-side аналог.

### 3.7 stats (`/api/v1/stats`)
| GET | `` **[member]** | сводная статистика: сырой SQL (CTE по `economy_tx`: staked/won/games по `kind LIKE 'casino_%'`, ферма по `kind='clicker_mint'`) + топ-10 крупнейших выигрышей из `casino_games` (payout > bet) → `{players: [PlayerStat{tg_id, username, fullname, balance, casino_net, casino_staked, casino_won, farm_earned, games_played}], biggest_wins: [BiggestWin]}` |

### 3.8 social (`/api/v1/social`) — «магазин» действий, постящих в чат
| GET | `/prices` **[member]** | → `{poke, joke, roast}` (константы POKE_COST/JOKE_COST/ROAST_COST) |
| POST | `/poke` **[member]** | `{target, kind: poke\|hug\|highfive}` → `ActionResp{text, cost, user_balance}` — бот отправляет сообщение в чат |
| POST | `/joke` **[member]** | `{topic: 2..120}` — AI-анекдот в чат |
| POST | `/roast` **[member]** | `{target}` — AI-прожарка в чат |

### 3.9 duel (`/api/v1/duel`) — PvP 1v1
| GET | `/list` **[member]** | входящие/исходящие вызовы |
| POST | `/challenge` **[member]** | `{opponent: "@username\|tg_id", stake>=1}` — сервис постит в чат кнопку-deep-link `t.me/<bot>?startapp=<chat_id>_duel` |
| POST | `/{duel_id}/accept` · `/decline` · `/cancel` **[member]** | действия по вызову |

### 3.10 tags (`/api/v1/tags`) — аренда кастомного тега (подпись в чате)
| GET | `/state` **[member]** | текущий тег юзера |
| GET | `/quote?days=N` **[member]** | → `{days, price}` |
| POST | `/rent` **[member]** | `{title: 1..16 симв., days: 1..7}` |
| POST | `/cancel` **[member]** | снять тег |

### 3.11 gacha (`/api/v1/gacha`)
| GET | `/collection` **[member]** | коллекция персонажей |
| POST | `/roll` **[member]** | `{count}` — крутка |
| POST | `/heroine` **[member]** | `{char_id}` — выбрать героиню для фермы |
| POST | `/banner` **[member][admin]** | `{char_id}` — сменить баннер |

### 3.12 feedback (`/api/v1/feedback`) — глобальный (без member-проверки!)
| POST | `` | `{kind: "bug"\|"idea", text: 5..2000}` → `{ok}` — ручная заявка |
| POST | `/assist` | `{message: 2..2000}` → `{reply, registered: {id, kind}\|null, degraded?}` — ИИ-форма: LLM отвечает и сам заводит заявку |

### 3.13 events (`/api/v1/events`) — SSE-пуш баланса
| GET | `/events` **[member]** | `StreamingResponse(media_type="text/event-stream")` |

Механика (эталонный паттерн для live-баланса):
- EventSource не умеет заголовки → initData и chat_id идут **query-параметрами** (`init_data`, `chat_id`) — require_auth это поддерживает.
- Подписка на Redis-канал `bal:{chat_id}` (`common/events.py: balance_channel`), фильтрация по `user_id` на сервере, клиенту летит `data: {"balance": N}`.
- Публикация: `common/events.py: publish_balance(user_id, chat_id, balance)` — вызывается сервисами **ПОСЛЕ commit**, best-effort (никогда не бросает; если REDIS_URL пуст — no-op).
- Несколько uvicorn-воркеров ок: Redis фанаутит всем подписчикам.
- Heartbeat `: ping\n\n` каждые 20с; без Redis — только heartbeat (чтобы клиент не циклил reconnect). Заголовки: `Cache-Control: no-cache`, `X-Accel-Buffering: no`, `Connection: keep-alive`.

### 3.14 analytics (`/api/v1/event`) — приём usage-событий
| POST | `/event` | `{event: str<=48, props?: dict}` → `{ok: true}` — best-effort запись через `analytics_service.record_event` |

### 3.15 admin (`/api/v1/admin`) — всё [admin]
| POST | `/balance_adjust` **[member]** | `{target, amount(±), note?}` → `{user_id, username, new_balance}`; + → `credit(kind="admin_adjust")`, − → `debit` |
| POST | `/bank_adjust` **[member]** | `{amount(±), note?}` → `{new_balance}`; `SELECT ... FOR UPDATE` на `chat_bank`, запрет ухода в минус, пишет `EconomyTx(user_id=None, kind="admin_bank_adjust")` |
| POST | `/markets/{id}/resolve` **[member]** | `{winning_option_position>=1}` — расчёт выплат |
| POST | `/markets/{id}/cancel` **[member]** | отмена рынка с рефандами |
| GET | `/analytics?hours=24` | usage-сводка (views/actions по событиям, clamp 1..720ч) |
| GET | `/metrics?reset=0` | перф-метрики API (per-route n/avg/max/p50/p95/err4/err5 + пул БД) из `common.metrics.snapshot` |
| GET | `/feedback` | открытые заявки + `default_reward(kind)` |
| POST | `/feedback/{fid}/close` | `{amount?: null=дефолт, 0=без награды}` — закрыть, начислить награду в чат, откуда заявка, уведомить автора через `send_chat_message`; повторное закрытие → 409 |

---

## 4. miniapp/ — SvelteKit SPA

### 4.1 Стек и сборка
- **Svelte 4 + SvelteKit 2 + TypeScript + Vite 5**, единственная runtime-зависимость — `lightweight-charts` ^4.2 (график курса фермы, динамический import).
- `svelte.config.js`: `@sveltejs/adapter-static` c `fallback: 'index.html'`, `strict: false`, `prerender: { entries: [] }` → чистая **SPA без SSR**, все страницы рендерятся на клиенте (роуты со `window.location` в разметке это подтверждают).
- `src/app.html`: `<script src="https://telegram.org/js/telegram-web-app.js"></script>` в `<head>` — официальный SDK Mini Apps; `lang="ru"`, `viewport-fit=cover`.
- `miniapp/Dockerfile` (multi-stage): `node:22-alpine` → `npm install && npm run build` (build-arg `VITE_API_BASE_URL`, дефолт `/api/v1` — относительный путь, т.е. API за тем же доменом через reverse-proxy) → `nginx:alpine` + `COPY build /usr/share/nginx/html`.
- `docker-compose.yml`: сервис `miniapp` порт `8003:80`, `api` порт `8002:8000`; env `MINIAPP_API_BASE_URL` прокидывается в build-arg. Снаружи маршрутизацию домена делает Dokploy (внешняя сеть `dokploy-network`).

### 4.2 nginx.conf (эталон для SPA Mini App)
```nginx
location / { try_files $uri $uri/ /index.html; }                 # SPA-fallback
location ~* \.(?:js|css|png|...)$ { expires 7d; add_header Cache-Control "public, max-age=604800, immutable"; }
location = /index.html { add_header Cache-Control "no-store, no-cache, must-revalidate"; }
error_page 500 502 503 504 /maintenance.html;                    # брендированная заглушка
location = /maintenance.html { internal; add_header Cache-Control "no-store"; }
```
`static/maintenance.html` — статическая страница «обновляемся».

### 4.3 Открытие из бота (/casino, deep-link) — bot/handlers/casino.py
**Важный практический вывод эталона:** в групповых чатах inline-кнопка с `WebAppInfo` не работает (`BUTTON_TYPE_INVALID`). Поэтому используется **deep-link Main Mini App**:
```python
link = f"https://t.me/{me.username}?startapp={chat_id}"   # или MINIAPP_DEEPLINK override
button = InlineKeyboardButton(text="Открыть казино", url=link)
```
Telegram открывает Main Mini App бота и кладёт значение `startapp` в `initDataUnsafe.start_param`. Формат start_param: `"<chat_id>"` или `"<chat_id>_<route>"` (например, `duel_service` шлёт кнопку с `startapp={chat_id}_duel` — deep-link сразу в раздел дуэлей). Env: `MINIAPP_DEEPLINK` — переопределение полного URL.

### 4.4 src/lib/* — клиентская инфраструктура

**tg.ts** — обёртка над `window.Telegram.WebApp` (в dev вне TMA — заглушки):
- `getInitData()` → `tg.initData`;
- `parseStartParam()` — regex `^(-?\d+)(?:_([a-z]+))?$` → `{chatId, route}`; chat_id валиден при `|id| > 100`;
- `getChatId()` — приоритет: start_param → query `?chat_id=`;
- `getStartRoute()` — whitelist роутов (`duel, farm, feedback, gacha, games, markets, shop, tags`);
- `tgReady()` — `tg.ready(); tg.expand();`
- `showAlert()`, `haptic('light'|'medium'|'heavy'|'success'|'error'|'warning')` → HapticFeedback.

**api.ts** — единая обёртка `request<T>(path, init)` (≈350 строк, ~60 типизированных методов `api.*`):
```ts
headers.set('X-Telegram-Init-Data', getInitData());
if (chatId != null && !url.searchParams.has('chat_id')) url.searchParams.set('chat_id', String(chatId));
```
- разбор ошибок FastAPI `extractError`: `detail` строка / массив 422 `[{msg}]` / объект → человекочитаемый текст;
- network error или статус 502/503/504 → `markDown()` (режим «обновляемся»); успешный ответ → `markUp()`;
- **сниффинг баланса**: из ответа `/me` — `seedFromMe`, `/balance` — `setBalance`, из любого другого — `sniffBalance` (ключи `user_balance | new_balance | user_balance_after | sender_balance`, банк из `bank | bank_after? bank_balance`) → глобальный store, экраны не перезапрашивают баланс;
- авто-трекинг: каждый успешный POST (кроме `/event`) → `track('action', {name: path})`.

**balance.ts** — `writable<{balance, bank, updatedAt}>`; сидируется один раз, дальше оптимистичные обновления из ответов + SSE.

**sse.ts** — `startBalanceSSE()`: один `EventSource` на сессию, URL `/api/v1/events?init_data=...&chat_id=...`; `onmessage` → `setBalance`; реконнект нативный EventSource, `onerror` глушится.

**service.ts** — store `serviceState: 'ok'|'updating'`; `markDown()` запускает поллинг `GET /api/v1/ping` каждые 4с до оживления → UX «Обновляемся 🛠️» вместо ошибок при редеплое.

**analytics.ts** — `track('view'|'action', props)`: fire-and-forget `fetch(..., {keepalive: true})` на `/event`, дедуп повторных view одного роута.

**types.ts** — интерфейсы, зеркалящие Pydantic-схемы (UserPublic, MeResponse, Market, FarmState{workers: FarmWorker[]}, GameResult и т.д.).

**format.ts** — `fmtCoins` (Intl ru-RU), `fmtDate`, `fmtPct`, `shortLabel`.

**changelog.ts** — патч-ноут как **статический TS-массив** `CHANGELOG: [{date, title, items[]}]` (без БД: правка файла → редеплой). Индикатор «Что нового ●» на главной: `CHANGELOG[0].date > localStorage.cl_seen`.

**Компоненты**: `BetInput.svelte` (число + пресеты 10/50/100/500/1000 + «all»), `UserPicker.svelte` (автокомплит по `GET /members` с debounce 220мс, клавиатурная навигация), `GameRules.svelte` (аккордеон «📖 Правила и выплаты» на каждой игре, включая схемы 10 линий слотов).

**app.css** — дизайн-система на CSS-переменных: светлая палитра в `:root`, тёмная через `@media (prefers-color-scheme: dark)` + принудительные классы `.tg-dark`/`.tg-light` (layout ставит их по `tg.colorScheme` — страховка от битых `--tg-theme-*`). Утилиты `.card`, `.h1/.h2`, `.muted/.danger/.success`, `.badge-{open|resolved|closed|cancelled}`.

**app.d.ts** — ручные типы `TelegramWebApp` (initData, initDataUnsafe.start_param, MainButton, BackButton, HapticFeedback, showAlert/showConfirm, colorScheme...).

### 4.5 Структура routes/ (все страницы)

```
/                      — хаб: карточка баланса (gradient) + сетка тайлов 2×N всех разделов; тайл «Админка» только при me.is_admin
/games                 — хаб игр (5 карточек с кратким RTP-описанием)
/games/slots           — слоты 5×3, 10 линий: CSS-анимация лент (offset+transition, каскадная остановка),
                         Web Audio (осцилляторы: джинглы, тики барабанов), фриспины, count-up выигрыша,
                         идемпотентный спин (crypto.randomUUID → idem_key, ключ держится до подтверждения)
/games/roulette        — европейская рулетка (number/color/parity/half/dozen)
/games/blackjack       — hit/stand/double по game_id (сервер держит состояние партии)
/games/dice            — over/under порог 1..99
/games/coinflip        — орёл/решка
/farm                  — кликер (794 строки): клиентский батчинг тапов (flush каждые 400мс, throttle 20 тапов/с),
                         оптимистичный displayCp = cp + pendingTaps*tap_level + автонакопление,
                         спрайты героини idle/tap/bonus (/static/farm/*.png), тап-бёрсты «+N»,
                         AMM: локальный расчёт constant-product (ammOut), кривая price-impact (SVG),
                         лестница котировок, график курса на lightweight-charts (динамический import, ResizeObserver)
/markets               — список рынков (фильтр статуса)
/markets/[id]          — детали + ставка: выбор опции, пресеты суммы, Telegram MainButton
                         («Поставить N на "..."» + showProgress) — эталон использования MainButton
/markets/create        — создать рынок (вопрос, опции, длительность)
/markets/import        — импорт Polymarket/Manifold по URL
/portfolio             — мои ставки
/leaderboard           — статистика: табы balance/casino/farm/wins из GET /stats
/history               — лента событий чата (пагинация offset+has_more)
/transfer              — перевод: UserPicker, клиентское зеркало формулы комиссии
                         Math.max(1, Math.ceil(amount*0.05)), топ-30 лидерборда как быстрый выбор
/shop                  — социальный магазин: poke/hug/highfive, AI-анекдот, AI-роаст (постят в чат)
/duel                  — дуэли: challenge/accept/decline/cancel
/gacha                 — крутка, коллекция, выбор героини
/tags                  — аренда тега (title до 16 симв., 1–7 дней)
/feedback              — ИИ-форма (чат с ассистентом /feedback/assist) + фолбэк «Отправить без ИИ»
/rules                 — правила экономики
/changelog             — патч-ноут из changelog.ts
/admin                 — хаб (проверка is_admin на клиенте, сервер дублирует 403)
/admin/balance         — ±гривны юзеру;  /admin/bank — ±банк чата
/admin/markets/manage  — resolve/cancel рынков
/admin/feedback        — закрытие заявок с наградой
/admin/analytics       — usage-аналитика + перф-метрики API
/dev/gacha             — dev-превью гача-артов
```

### 4.6 +layout.svelte — корневой каркас (заимствовать паттерны)
- `tgReady()` (ready+expand), запуск `startBalanceSSE()`;
- баннер-предупреждение, если нет ни `?chat_id`, ни `start_param` («Открой Mini App из чата через /casino»);
- трекинг `view` на каждый переход (`page.subscribe`);
- deep-link: если открыто на `/` и в start_param есть route → `goto(route + window.location.search)`;
- классы `.tg-dark/.tg-light` по `tg.colorScheme`;
- **Telegram BackButton**: на не-главных роутах show + `history.back()`, на главной hide + `tg.close()`;
- оверлей «Обновляемся 🛠️» при `serviceState === 'updating'`.
- **Проброс chat_id между страницами**: каждая ссылка конкатенирует `window.location.search` (`href={'/farm' + window.location.search}`) — chat_id живёт в query всю сессию.

---

## 5. Переменные окружения зоны

| Env | Где | Назначение |
|---|---|---|
| `TELEGRAM_TOKEN` | api/auth.py | HMAC initData + getChatMember |
| `TMA_INIT_DATA_MAX_AGE` | api/auth.py | TTL initData, дефолт 86400с |
| `TMA_MEMBERSHIP_CACHE_TTL` | api/auth.py | кэш членства, дефолт 300с |
| `BOT_ADMIN_IDS` | api/auth.py | csv tg_id админов |
| `API_CORS_ORIGINS` | api/main.py | csv origins, дефолт `*` |
| `API_WORKERS` | api/Dockerfile | воркеры uvicorn, дефолт 3 |
| `REDIS_URL` | common/events.py, events.py | pub/sub баланса (пусто → SSE только heartbeat) |
| `MINIAPP_API_BASE_URL` → build-arg `VITE_API_BASE_URL` | compose/miniapp Dockerfile | база API фронта, дефолт `/api/v1` |
| `MINIAPP_DEEPLINK` | bot/handlers/casino.py | override deep-link Mini App |
| `CLICKER_MAX_CPS` | clicker_service | серверный кэп тапов, дефолт 30 |

---

## 6. Паттерны, которые стоит заимствовать в Yuvi Bot v2

1. **API как тонкая обёртка над сервисами бота** (`asyncio.to_thread(sync_service_fn, ...)` + единый маппер доменных исключений в HTTPException) — ноль дублирования логики между ботом и Mini App.
2. **Авторизация**: заголовок `X-Telegram-Init-Data` + `chat_id` query; HMAC c `hmac.compare_digest`; `getChatMember` с TTL-кэшем как проверка членства; фолбэк initData из query для SSE.
3. **Deep-link вместо WebAppInfo** для групп: `t.me/<bot>?startapp=<chat_id>[_route]` + parse `start_param` на фронте + whitelist роутов.
4. **Live-баланс**: Redis pub/sub (`bal:{chat_id}`, publish после commit, best-effort) → SSE → svelte store + сниффинг баланса из любых ответов API.
5. **Идемпотентность спинов** (`idem_key` с клиентским UUID, держится до подтверждения) и **серверный кэп тапов** (MAX_CPS × elapsed) — обязательны для казино/кликера.
6. **UX деградации**: `GET /ping` + store `ok/updating` + оверлей «Обновляемся» + nginx `error_page → maintenance.html` — редеплой без пугающих ошибок.
7. **SPA-сборка**: adapter-static + fallback index.html + nginx try_files; `VITE_API_BASE_URL=/api/v1` относительным путём (один домен, без CORS-боли).
8. **История прозрачности**: единая таблица `economy_tx(kind, note)` + скрытие служебных `*_to_bank/*_from_bank` зеркал; `limit+1` для `has_more`.
9. Перф-middleware с per-route p50/p95 и снапшотом пула БД + `/admin/metrics` — дешёвый observability без Prometheus.
10. Статический changelog в TS-файле + `localStorage`-метка «есть новое».
