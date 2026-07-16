# Отчёт: bot/services/ в эталоне xyloz_tg_bot

Зона анализа: `bot/services/` репозитория **Heide172/xyloz_tg_bot** (эталон для Yuvi Bot v2).
Репозиторий не был найден локально — склонирован во время анализа в:
`C:\Users\root\AppData\Local\Temp\claude\C--Users-root-Desktop-YUVI-BOT\327a8fc2-b8d8-4c65-9d50-2d3d3ac0d69d\scratchpad\xyloz_tg_bot`

Всего 34 файла-сервиса, ~8400 строк. Стек: Python, aiogram (бот), FastAPI (`api/` для Mini App), SQLAlchemy + PostgreSQL (+pgvector), APScheduler, Redis (pub/sub → SSE), OpenAI SDK поверх OpenCode Go (LLM), self-hosted cobalt (скачивание медиа), отдельный NLP-микросервис (`http://nlp:8000`, sentiment/toxicity/embeddings).

---

## 0. Общие архитектурные паттерны сервисного слоя

1. **Сервисы — чистые модули-функции, без классов-синглтонов.** Каждый сервис открывает СВОЮ `SessionLocal()` (из `common.db.db`), делает работу, `commit()`/`rollback()`, `close()` в `finally`. Никакого DI.
2. **Соглашение `*_sync`:** блокирующие функции именуются `..._sync` и вызываются из async-кода через `asyncio.to_thread(...)`. Один и тот же сервис используется и aiogram-хендлерами (`bot/handlers/`), и FastAPI-роутами (`api/routes/`) Mini App.
3. **Единый денежный контур:** ВСЕ движения денег проходят через три примитива, определённых в `markets_service.py` и реимпортируемых всеми остальными: `_get_or_create_balance(session, user_id, chat_id)` (SELECT ... FOR UPDATE по `user_balance`), `_get_or_create_bank(session, chat_id)` (FOR UPDATE по `chat_bank`), `_log_tx(session, user_id|None, chat_id, amount, kind, ref_id, note)` (append в `economy_tx`). Иерархия исключений тоже общая: `MarketError` → `InsufficientFunds`, `InvalidArgument`, `MarketNotFound`, `MarketClosed`; `CasinoError`, `ClickerError`, `DuelError` наследуют `MarketError`.
4. **Пессимистические блокировки** `with_for_update()` на всех строках-балансах и объектах игр перед изменением — это защита от гонок при параллельных запросах Mini App.
5. **RNG для денег — `secrets.SystemRandom()`** (казино, гача, дуэли, ежедневный пик), не `random`.
6. **Побочные эффекты (Telegram API, Redis) — ВНЕ транзакции**, после `commit()`. Часть сервисов зовёт Bot API «сырым» `urllib.request` (не aiogram), потому что работает и из API-контейнера без aiogram-бота (`tag_rental_service`, `social_service`, `duel_service`).
7. **Пуш баланса в Mini App:** `common/events.py` → `publish_balance(user_id, chat_id, balance)` — Redis PUBLISH в канал `bal:{chat_id}` с JSON `{"user_id", "balance"}`; best-effort (Redis недоступен → молча пропускаем), API читает и стримит по SSE.
8. **Все константы — env-tunable** через `int(os.getenv("NAME", "default"))` на уровне модуля.
9. **`BotSetting` (key-value таблица `bot_settings`)** — универсальное хранилище настроек/кэшей: модель LLM, системный промпт, баннер гачи, кэш анекдота/фразы дня, держатели номинант-тегов (`nomtag:{slot}:{chat_id}` → `tg_id:YYYY-MM-DD`). Многие сервисы делают ленивый `BotSetting.__table__.create(bind=engine, checkfirst=True)`.
10. **Деньги в двух «валютах»:** гривна (int, `user_balance.balance`) и cp (click points, `clicker_farms.cp_balance`, BIGINT). Курс между ними — живой AMM-пул на чат.

Экономические роли (эмиссия/sink):
- **mint (эмиссия из воздуха):** стартовый бонус, награды номинаций, награда за фидбек, продажа cp на AMM (`clicker_mint`).
- **sink (в банк чата `chat_bank`):** комиссия перевода 5%, комиссия создания/импорта/резолюции рынков, комиссия дуэлей 5%, все ставки казино, стоимость гача-круток, аренда тегов, соц.магазин, скачивание медиа.
- **из банка:** выплаты казино (банк МОЖЕТ уйти в минус — «корректный P&L, отыграется на house edge»).

---

## 1. Экономика — `economy_service.py` (380 строк)

Таблицы: `user_balance` (PK `(user_id, chat_id)`, `balance int`, индекс `idx_user_balance_chat`), `chat_bank` (PK `chat_id`, `balance int`), `economy_tx` (`id, user_id nullable, chat_id, amount int(+/-), kind varchar(40), ref_id varchar(80), note text, created_at`; индексы `idx_economy_tx_user_chat`, `idx_economy_tx_chat_kind`). `user_id=NULL` в tx = операция банка.

ENV: `ECONOMY_START_BONUS=1000`, `TRANSFER_FEE_PCT=5`, `TRANSFER_FEE_MIN=1`.

API:
- `get_balance(user_id, chat_id, auto_start=True) -> int` — при первом обращении начисляет стартовый бонус (kind=`start_bonus`); `IntegrityError` (юзер не создан) → rollback, вернуть 0.
- `credit(...)/debit(...) -> int` — атомарные пополнение/списание + `_log_tx` + `_publish` (Redis) после commit; `debit` бросает `InsufficientFunds`.
- `transfer(from,to,chat,amount)` — без комиссии (внутренний kind=transfer).
- `transfer_with_fee(...) -> dict{amount,fee,total,sender_balance,receiver_balance}` — отправитель платит `amount+fee`, `fee=max(TRANSFER_FEE_MIN, ceil(amount*PCT/100))` → банк; 3 записи tx: `transfer_out`/`transfer_in`/`transfer_fee`.
- `deposit_to_bank / withdraw_from_bank / user_to_bank(kind→ kind_user + kind_bank две записи)`.
- Read-only: `get_chat_bank`, `leaderboard(chat_id, limit=10) -> [(User, balance)]`, `chat_economy_summary -> {users, total_in_users, bank, total_supply, last_txs}`, `resolve_user_by_username` (case-insensitive без @).

Ключевой паттерн (двойная запись + журнал):
```python
sender.balance -= total; receiver.balance += amount; bank.balance += fee
_log_tx(session, from_user_id, chat_id, -total, kind="transfer_out", ref_id=str(to_user_id))
_log_tx(session, to_user_id, chat_id, amount, kind="transfer_in", ref_id=str(from_user_id))
_log_tx(session, None, chat_id, fee, kind="transfer_fee")
session.commit()
_publish(from_user_id, chat_id, sb)  # Redis ПОСЛЕ commit
```
Используется: `api/routes/economy.py` (баланс/перевод/лидерборд Mini App), `bot/handlers/messages.py` (`get_balance`), `nominations_service` (credit наград).

---

## 2. Рынки ставок parimutuel — `markets_service.py` (515 строк)

Таблицы: `markets` (`id, chat_id, type='internal'|'polymarket'|'manifold', question text, creator_id FK users, status open|closed|resolved|cancelled, closes_at, resolved_at, winning_option_id, external_url, external_id varchar(120)`), `market_options` (`market_id FK, label varchar(200), pool int, position`), `bets` (`market_id, option_id, user_id, amount, payout, refunded 0/1, created_at`).

ENV/константы: `MARKET_CREATION_FEE=100`, `MARKET_RESOLUTION_FEE_PCT=5`, `MARKET_MIN_BET=10`, опций 2..6, вопрос 5..400 симв., длительность 5 мин..365 дней.

API:
- `parse_duration("7d"|"12h"|"90m") -> timedelta`.
- `create_market(chat_id, creator_user_id, question, options, duration) -> CreatedMarket(market_id, fee_charged, options=[(id,label)])` — комиссия создателя → банк (tx `market_create_fee_user`/`market_create_fee_bank`).
- `place_bet(market_id, option_position 1-based, user_id, amount) -> dict` — FOR UPDATE на market и опции; если `closes_at` в прошлом — переводит в `closed` и бросает `MarketClosed`; списывает ставку, `option.pool += amount`, tx `bet_place`.
- `resolve_market(market_id, winning_option_position) -> dict{winning_label, total_pool, winner_pool, commission, distributed, payouts[], refunded}` — логика: total_pool=0 → просто resolved; winner_pool=0 → **рефанд всем** (`bet_refund`, `bet.refunded=1, payout=amount`); иначе `commission=int(total*5%)` → банк (`market_resolve_fee_bank`), остальное победителям **пропорционально ставке**: `payout = int(distributable * bet.amount / winner_pool)` (tx `bet_payout`). Округление вниз — «пыль» остаётся несведённой (осознанно).
- `cancel_market(market_id)` — рефанд всех незарефандженных ставок.
- Read: `get_market -> MarketView(market, options, total_pool, bets_count)`, `list_markets(chat_id, status, limit=20)`, `user_open_positions(chat_id, user_id) -> list[dict]` (портфель).
- `auto_close_expired() -> int` — планировщик каждые 5 минут переводит open→closed по `closes_at`.

Используется: `api/routes/markets.py`, `api/routes/portfolio.py`, `api/routes/admin.py` (резолюция/отмена админом).

---

## 3. Импорт внешних рынков — `external_markets.py` (391 строка)

Polymarket + Manifold, авторезолюция. ENV: `EXTERNAL_MARKETS_HTTP_TIMEOUT=30`, `MARKET_IMPORT_FEE=50`.

- `parse_url(url) -> ("polymarket"|"manifold", slug)|None`. Regex: `polymarket\.com/(?:event|market)/([\w-]+)(?:/([\w-]+))?` (второй slug = под-рынок event'а), `manifold\.markets/[\w_.\-]+/([\w-]+)`.
- `fetch_external_market(url) -> ExternalMarketData(source, external_id, external_url, question, options, close_time, is_resolved, resolution)`.
- Polymarket: `GET https://gamma-api.polymarket.com/markets?slug={slug}&limit=1`; `outcomes`/`outcomePrices` приходят JSON-строками — двойной `json.loads`; resolved → победитель = опция с max price. Если slug оказался event-категорией (`/events?slug=`) — понятная ошибка «дай ссылку на под-рынок» (parimutuel не мапится на N независимых Yes/No).
- Manifold: `GET https://api.manifold.markets/v0/slug/{slug}`; BINARY → Yes/No по `resolution` YES/NO; MULTIPLE_CHOICE → до 6 answers, победитель по `a.id == resolution`; `closeTime` в мс.
- `import_market(chat_id, creator_user_id, url)` — async, fetch + `asyncio.to_thread(_create_imported_market_sync)`: комиссия 50 → банк (`market_import_fee_user/bank`), **дедуп по (chat_id, type, external_id)** → `{"already_imported": True}`, market создаётся с `type=source`, `external_url`, `external_id`.
- `auto_resolve_external() -> {checked, resolved, errors}` — планировщик каждые 30 мин: для всех рынков `type in (polymarket, manifold), status in (open, closed)` перезапрашивает источник; если `is_resolved` и resolution матчится по label (lower/strip) на позицию опции → `resolve_market(...)` (обычная parimutuel-выплата).

---

## 4. Казино — `casino_service.py` (796 строк)

Таблица `casino_games`: `chat_id, user_id, game varchar(20), bet, payout (gross), status active|finished|cancelled, outcome win|lose|push|blackjack, state JSONB (для blackjack: deck/player/dealer/doubled), created_at, finished_at, idem_key varchar(40)`.

ENV: `CASINO_MIN_BET=10`, `CASINO_MAX_BET=100000`. `HOUSE_EDGE_PCT=2.0`. RNG: `secrets.SystemRandom()`.

**Сердце — `_settle_sync(chat_id, user_id, game, bet, outcome, payout, details, max_potential_payout, idem_key=None) -> GameResult`:**
- **Идемпотентность:** если `idem_key` задан и уже есть `CasinoGame(user_id, idem_key)` — вернуть результат прежней партии без повторного списания (ретрай после потери HTTP-ответа). На гонку — ловим `IntegrityError` (уникальный индекс), rollback, перечитываем в новой сессии.
- Атомарно: ставка user→bank (tx `casino_{game}_bet` + `casino_{game}_bet_to_bank`), запись `CasinoGame(status=finished)`, выплата bank→user (tx `casino_{game}_payout_from_bank` + `casino_{game}_payout`). **Выплата НЕ обрезается остатком банка** — банк может уйти в минус (математический RTP важнее).
- `GameResult(game_id, game, outcome, bet, payout, net, user_balance_after, bank_after, details)`.

Игры:
- `play_coinflip_sync(chat, user, bet, pick 'heads'|'tails')` — win → 1.98x.
- `play_dice_sync(..., mode 'over'|'under', threshold 1..99)` — roll 1-100, `multiplier = round((1 - 0.02) / win_prob, 2)`, шанс должен быть 1..99%.
- `play_slots_sync(..., idem_key)` — 5×3, 10 paylines (`SLOT_LINES`), символы `cherry lemon bell star diamond wild scatter`, веса `[26,24,20,13,7,4,6]`. Wild подменяет всё; выплата слева-направо 3/4/5 в ряд × (bet/10) по `SLOT_PAYTABLE` (low-variance, RTP ~94.2%). Scatter ≥3 платит от общей ставки (`{3:2,4:4,5:10}×bet`) и даёт `{3:4,4:5,5:7}` фриспинов ×2, которые **авто-проигрываются на сервере** и кладутся в `details.freespins` (фронт анимирует). `max_potential = bet*50`.
- `play_roulette_sync(..., bet_type, value)` — европейская 0-36; `number`→36x, `color/parity/half`→2x, `dozen`→3x; `RED_NUMBERS` — стандартный сет.
- Blackjack многоходовый: `start_blackjack_sync` (эскроу ставки, natural BJ сразу сеттлится 2.5x; иначе `status=active`, колода/руки в `state` JSONB, ответ содержит `dealer_visible`, `can_double`), `hit_blackjack_sync(game_id, user_id)` (FOR UPDATE, ≥21 → авто-сеттл), `stand_blackjack_sync`, `double_blackjack_sync` (только на 2 картах, вторая ставка списывается, одна карта, авто-стенд). `_settle_blackjack`: дилер добирает до 17 (stand on soft 17), payout_mult: BJ 2.5 / win 2.0 / push 1.0; при doubled `bet_used = bet*2`.

Используется: `api/routes/games.py` (Mini App), команда `/casino` (bot/handlers/casino.py) лишь открывает Mini App deep-link'ом.

---

## 5. Ферма-кликер — `clicker_service.py` (450 строк)

Таблица `clicker_farms`: `user_id+chat_id (uq)`, `cp_balance BIGINT`, `tap_level (default 1)`, `auto_level`, `workers JSONB {"cherry":lvl,...}` (legacy), `lifetime_cp`, `pity_ssr`, `pity_ur`, `gacha_rolls`, `active_heroine varchar(40)`, `gacha_migrated 0/1`, `last_seen_at`.

ENV: `CLICKER_OFFLINE_CAP_HOURS=4`, `CLICKER_AUTO_RATE=0.5` cp/сек/уровень, `CLICKER_TAP_UPGRADE_BASE=50`, `CLICKER_AUTO_UPGRADE_BASE=200`, `CLICKER_UPGRADE_GROWTH=1.15`, `CLICKER_MAX_CPS=30` (серверный кэп; клиент троттлит на 20), `CLICKER_MAX_TAP_LEVEL=50`, `CLICKER_MAX_AUTO_LEVEL=100`, работницы `CLICKER_W_{TYPE}_RATE/COST` (cherry 0.2/50 … diamond 20/30000), `CLICKER_MAX_WORKER_LEVEL=50`, тиры арта `CLICKER_WORKER_TIER_T2=10 / T3=25`.

Формулы:
- Стоимость апгрейда: `int(round(base * growth**level))`.
- Пассивный доход `_passive_rate` = `(Σ legacy WORKER_RATE[t]*lvl + Σ gacha worker base_value*star_mult) * heroine_mult + auto_level*AUTO_RATE`.
- **Оффлайн-накопление `_accrue_offline(session, farm, now)`:** `income = int(min(elapsed_sec, CAP_hours*3600) * rate)`; вызывается в начале КАЖДОЙ операции, обновляет `last_seen_at`. Дальше кэпа «ферма засыхает».
- Тап: `tap_sync(user, chat, count, elapsed_ms)` — анти-чит: `accepted = min(count, max(1, int(MAX_CPS*elapsed_ms/1000)))`; `gain = accepted * tap_level * heroine_mult`.

Операции (все возвращают полный `FarmState` dataclass: cp_balance, tap_level, auto_level, auto_rate_cps, next_tap_cost, next_auto_cost, bank_balance, user_balance, lifetime_cp, cp_per_hryvnia, offline_cap_seconds, workers[]): `get_state_sync`, `tap_sync`, `upgrade_tap_sync`, `upgrade_auto_sync`, `hire_worker_sync(wtype)` (JSONB: обязательно `farm.workers = dict(workers)` — переприсвоить, чтобы SQLAlchemy заметил), `convert_sync(cp_amount)` — продажа cp через AMM (`market_service.sell_cp`), гривны эмитируются юзеру (tx `clicker_mint`), `buy_cp_sync(hryvnia_amount)` — обратный поток (tx `clicker_buy_cp`), `wipe_farm_sync(chat_id|None)` — ручной админ-вайп (`DELETE FROM clicker_farms/gacha_collection/clicker_market_pool/clicker_market_price`).

Используется: `api/routes/clicker.py`; `/farmwipe` (bot/handlers/farm_admin.py).

## 5a. AMM-рынок cp↔гривна — `market_service.py` (202 строки)

Constant-product пул пер-чат вместо фикс. курса и дневных кэпов. Таблицы: `clicker_market_pool` (PK chat_id, `r_cp float`, `r_h float`), `clicker_market_price` (снапшоты `ts, rate` для графика).

ENV: `MARKET_ANCHOR_RATE=100` (cp за гривну в равновесии), `MARKET_R_H0=200000` (глубина в гривнах; R_cp0 = R_h0×anchor), `MARKET_TAU_MIN=240`, `MARKET_TICK_MIN=10`, `MARKET_PRICE_RETAIN_DAYS=7`.

```python
def sell_cp(session, chat_id, cp):        # ферма продаёт cp → гривны, цена вниз
    k = pool.r_cp * pool.r_h
    out = pool.r_h - k / (pool.r_cp + cp) # slippage встроен
    pool.r_cp += cp; pool.r_h -= int(out)
def recover_and_snapshot_all():           # тик каждые TICK_MIN минут
    factor = math.exp(-TICK_MIN / TAU_MIN)
    p.r_cp = r_cp0 + (p.r_cp - r_cp0) * factor  # mean-reversion к якорю
```
`buy_cp` — зеркально; `quote_sync(chat_id)` — котировка; `price_history(chat_id, limit=200)` — для графика; `pool_snapshot` — чтение БЕЗ FOR UPDATE (для read-путей). Рынок «сам себе тормоз» — никаких подушевых лимитов.

---

## 6. Гача — `gacha_service.py` (358) + `gacha_catalog.py` (75)

Таблица `gacha_collection`: `user_id+chat_id+char_id (uq)`, `stars 1..5`, `copies`, `obtained_at`. Pity-счётчики живут в `clicker_farms.pity_ssr/pity_ur`.

Каталог фиксирован в коде: `GachaChar(id, name, rarity R|SR|SSR|UR, role worker|heroine, base_value, asset)`; `star_mult(stars) = 1.0 + 0.25*(stars-1)` (5★=2.0). R — 5 legacy-работниц; SR — 4 воркера (8..16 cp/с); SSR — 2 воркера (35/45) + героиня ×1.5; UR — 3 героини (×2.0/×2.5/×3.0). `BY_RARITY`, `LEGACY_WORKER_MAP`, `DEFAULT_HEROINE_MULT=1.0`.

ENV: `GACHA_ROLL_COST=300` гривен, `GACHA_X10_COST=ROLL_COST*9` (скидка), `GACHA_SSR_PITY=50`, `GACHA_UR_PITY=90`, `GACHA_BANNER_RATEUP=0.5`. `BASE_WEIGHTS={"SR":0.80,"SSR":0.18,"UR":0.02}` (R из ролла исключён). `DUP_REFUND={"R":20,"SR":80,"SSR":300,"UR":1500}` гривен за дубль при 5★.

Механики:
- `_pick_rarity(farm)`: инкремент обоих pity; `pity_ur>=90 → UR`; `pity_ssr>=50 → SSR`; иначе рулетка по weights. `_apply_pity_reset`: UR сбрасывает оба, SSR — только ssr.
- `_pick_char(rarity)`: для UR с шансом 0.5 — текущий баннер (`BotSetting['gacha_banner']`, `get_banner()/set_banner()` — только UR).
- `_grant`: новый чар → 1★; дубль → +1★ до 5; сверх 5★ → refund гривнами. После insert обязателен `session.flush()` (повтор того же чара в x10 иначе даст UniqueViolation).
- `roll_sync(user, chat, count 1|10) -> dict{results[{char_id,name,rarity,stars,new,refund,asset}], spent, refunded, pity_ssr, pity_ur, user_balance}`. Цена → банк (tx `gacha_roll`/`gacha_roll_to_bank`); **x10-гарант:** если все 10 — ниже SR, последний результат заменяется случайным SR; суммарный refund дублей возвращается (tx `gacha_dup_refund`).
- Доход фермы: `farm_multipliers(session, user, chat) -> (gacha_worker_raw, heroine_mult, active_heroine)` — Σ base_value×star_mult по SR/SSR/UR-воркерам; героиня активна только если `role=heroine` и есть в коллекции. `set_heroine_sync(char_id)`.
- `ensure_migrated(session, farm)` — legacy-работницы НЕ конвертируются (двойной системы дохода не ломаем), только чистка ошибочных R-записей; флаг `gacha_migrated`.
- `collection_sync` — весь каталог + owned/stars + pity + цены (экран коллекции Mini App).

Используется: `api/routes/gacha.py`; `clicker_service` (доход).

---

## 7. Дуэли — `duel_service.py` (307 строк)

Таблица `duels`: `chat_id, challenger_id, opponent_id, stake, status pending|resolved|declined|cancelled, winner_id, commission, created_at, resolved_at`.

ENV: `DUEL_MIN_STAKE=10`, `DUEL_MAX_STAKE=100000`, `DUEL_FEE_PCT=5`.

- `challenge_sync(challenger, chat, opponent, stake)` — **эскроу** ставки challenger'а сразу (tx `duel_stake_hold`); дедуп: один pending-вызов на пару; после commit — анонс в чат «сырым» Bot API `sendMessage` с inline-кнопкой deep-link `https://t.me/{bot}?startapp={chat_id}_duel` (username бота кэшируется через `getMe`).
- `accept_sync(duel_id, opponent_id)` — эскроу оппонента, `pool=stake*2`, `commission=max(1, ceil(pool*5%)) → банк` (tx `duel_fee`), **50/50 coinflip** `_rng.choice([challenger, opponent])`, приз победителю (tx `duel_win`), status=resolved. Возвращает dict + `prize`, `you_won`.
- `decline_sync` / `cancel_sync` — рефанд эскроу challenger'у (tx `duel_refund`).
- `list_sync(user, chat) -> {incoming, outgoing, history(15), me}`.

**Мута проигравшего в эталоне НЕТ** (это фича старого Yuvi-bot — реализовывать заново через `restrictChatMember`). Используется: `api/routes/duel.py`.

---

## 8. Ежедневные розыгрыши и номинации

### `daily_pick_service.py` (112)
Таблица `daily_pick`: `chat_id, day_msk date, winner_tg_id, title, picked_by_tg_id`. `pick_participant_of_day(chat_id, picked_by_tg_id=None) -> PickResult(day_msk, candidates_day_msk, winner_tg_id, winner_username, winner_fullname, is_new)`. Идемпотентно за MSK-день: повторный вызов возвращает существующего победителя (`is_new=False`). Кандидаты = distinct `User.tg_id`, писавшие **вчера** (окно naive-MSK, т.к. created_at в session TZ Europe/Moscow). Выбор `secrets.choice`. Вызывается командой `/fag` (bot/handlers/messages.py) и авто в 10:00 из nominations.

### `nominations_service.py` (382)
ENV: `NOMINATION_PRIZE=300`, `NOMINATION_FAG=500`, `NOMINATION_MIN_MESSAGES=5`, `NOMINATION_MIN_QUOTE_CHARS=30`, `NOMINATION_MIN_QUOTE_REACTIONS=2`, `NOMINATION_ACTIVE_WINDOW_DAYS=14`.
- 4 номинации за вчера: `pick_most_active` (count сообщений), `pick_most_toxic` (avg `Message.toxicity_score`, ≥5 сообщ.), `pick_most_positive` (доля `sentiment_label='positive'`), `pick_best_quote` (max реакций на текст ≥30 симв.). Зависят от NLP-разметки сообщений.
- **Идемпотентность наград:** `ref_id = f"{kind}:{chat_id}:{YYYY-MM-DD}"`; `_already_awarded` проверяет наличие `EconomyTx.ref_id`, потом `economy_service.credit(...)`. Kind'ы: `nomination_most_active` и т.д., `nomination_fag`.
- `run_daily_nominations(bot)` (cron 10:00 MSK): для каждого активного чата — `_auto_fag` (пик «пидора дня» + `award_fag` 500 + `assign_nomination_tag(bot, chat, tg_id, "пидор дня", slot="fag")`) + начисление 4 номинаций + пост-сводка в чат.

---

## 9. Теги custom_title и рынок аренды

### `tag_service.py` (215)
Трюк Telegram: custom_title только у админов, ≤16 символов ⇒ `set_title(bot, chat_id, tg_user_id, title)` = `promote_chat_member(can_invite_users=True, всё остальное False)` + `set_chat_administrator_custom_title`; `clear_title` = promote со всеми False (demote). Аiogram Bot (бот-контейнер).
- `assign_nomination_tag(bot, chat, tg_id, title, slot)` — вешает тег номинанту, снимая с предыдущего держателя слота (хранится в `BotSetting` под ключом `nomtag:{slot}:{chat_id}`, значение `tg_id:YYYY-MM-DD`); если у прежнего есть активная аренда — возвращает ему арендный тег вместо снятия. Приоритет у номинанта.
- `expire_nomination_tags(bot)` (cron 00:05 MSK) — снимает теги за прошлые дни, возвращая арендные.

### `tag_rental_service.py` (274)
Таблица `tag_rentals`: `chat_id, user_id, tg_user_id, title, price_paid, rented_at, expires_at, status active|cancelled|expired`. ENV: `TAG_RENT_PER_DAY=500`, `ALLOWED_DAYS=[1,3,7]`, `TITLE_MAX=16`.
- `rent_sync(user_id, tg_user_id, chat_id, title, days)` — уникальность title среди active в чате; один active на юзера (повторная аренда = продление/замена: перезапись title, `expires_at=now+days`, `price_paid+=`); цена → банк (tx `tag_rent`/`tag_rent_to_bank`); Telegram-вызов (raw urllib `promoteChatMember`+`setChatAdministratorCustomTitle`) — после commit.
- `cancel_sync`, `state_sync -> {per_day, allowed_days, max_len, mine{title,expires_at,expired}, occupied[]}`, `active_title_for_tg(chat,tg) -> str|None` (для взаимодействия с номинант-тегами), `expire_due_sync()` (планировщик каждые 5 мин: status→expired + `_clear_tg_title`).

Используется: `api/routes/tags.py`.

---

## 10. Социальный магазин — `social_service.py` (140)

ENV: `SOCIAL_POKE_COST=50`, `SOCIAL_JOKE_COST=150`, `SOCIAL_ROAST_COST=300`. Всё — sink в банк чата.
- `_charge(user, chat, amount, kind, note)` — атомарное списание→банк (tx `social_poke`/`social_joke`/`social_roast` + `_to_bank`).
- `do_poke(user, chat, actor, target, kind)` — шаблоны `POKE_TEMPLATES` (poke/hug/highfive), пост в чат.
- `do_joke(user, chat, actor, topic)` — LLM-анекдот по теме (модель `get_summary_model()`); при сбое ИИ деньги НЕ возвращаются («деньги ушли в банк чата, спасибо»).
- `do_roast(...)` — AI-прожарка участника (системный промпт «ведущий комеди-прожарки, по-доброму едко»).
- `send_chat_message(chat_id, text)` — raw Bot API sendMessage; переиспользуется feedback-сервисами для уведомления админов в ЛС.

Ленивый импорт `ai_client` внутри функции (openai тяжёлый — API стартует без него). Используется: `api/routes/social.py`, `feedback_service`.

---

## 11. Скачивание медиа — `media_dl_service.py` (257)

Self-hosted **cobalt** (`COBALT_API_URL=http://cobalt:9000/`, docker-сервис в compose). ENV: `MEDIADL_COST=50`, `MEDIADL_MAX_MB=48` (лимит Bot API 50МБ), `MEDIADL_CAPTION_MAX=600`.
- `URL_RE` ловит tiktok.com / instagram.com / youtube.com/shorts / youtu.be (+vm./vt./m. поддомены). `extract_url(text)`.
- `charge(user, chat)` — списание → банк (tx `mediadl_charge`); `refund(user, chat)` — возврат при неудаче (tx `mediadl_refund`). Паттерн в хендлере: charge → download → при ошибке refund.
- `_cobalt_resolve(url) -> (items, caption, error)`: POST `{url, videoQuality: "720"}`; статусы cobalt: `tunnel|redirect` → 1 файл, `picker` → карусель (до 10 = лимит media group), `local-processing` → отказ; маппинг кодов ошибок в человекочитаемые русские сообщения (youtube-антибот, приватное видео и т.п.). `caption` — их патч cobalt (текст поста).
- `_fetch_one` — стрим-скачивание во временный файл с обрывом при превышении MAX_BYTES; тип photo/video по Content-Type.
- `download_sync(url) -> (items[(path, type)], caption, error)` — вызывать в `asyncio.to_thread`.

Используется: `bot/handlers/media_dl.py` (реагирует на ссылки в сообщениях).

---

## 12. Фидбек — `feedback_service.py` (189) + `feedback_ai_service.py` (95)

Таблица `feedback`: `user_id, chat_id, kind bug|idea, text, status new|seen|done, reward, rewarded_at, created_at`. ENV: `FEEDBACK_REWARD_BUG=500`, `FEEDBACK_REWARD_IDEA=300`, `BOT_ADMIN_IDS` (csv tg_id).
- `create_feedback(user_id, chat_id, kind, text, who) -> id` — создать + уведомить всех админов в ЛС (`send_chat_message`).
- `close(fid, amount=None)` — **идемпотентно** (FOR UPDATE; `status=done` → `already_done`); награда = дефолт по типу / явная / 0; начисление — mint напрямую в баланс автора + tx `feedback_reward` (банк не трогаем). Возвращает данные для уведомления автора.
- `list_open`, `get_one`. Админ-команда `/fb` (bot/handlers/feedback_admin.py) и `api/routes/admin.py`.
- `feedback_ai_service.assist(user_id, chat_id, who, message)` — ИИ-саппорт формы: системный промпт с **грунтингом** (список фич продукта, чтобы не галлюцинировал), требует строгий JSON `{"reply": "...", "register": {"kind","text"}|null}`; парсер срезает ```-fence и ищет `{...}` регекспом; сбой → `degraded: true` (фронт показывает обычную форму); если register валиден — авто-`create_feedback`.

**Telegram Stars в эталоне НЕТ** (нет XTR/send_invoice/pre_checkout ни в bot/, ни в api/). Для Yuvi v2 паттерн идемпотентности брать из казино (`idem_key` + unique index + возврат прежнего результата) и номинаций (`ref_id` в `economy_tx` = `telegram_payment_charge_id`).

---

## 13. AI-инфраструктура

### `ai_client.py` (143)
Единый LLM-клиент: OpenAI SDK поверх **OpenCode Go** (`OPENCODE_BASE_URL=https://opencode.ai/zen/go/v1`, `OPENCODE_API_KEY`). ENV: `AI_MAX_OUTPUT_TOKENS=16000`, `AI_CALL_TIMEOUT_SEC/AI_STREAM_TIMEOUT_SEC=300`. **Все вызовы через stream** (обход Cloudflare 524 на медленных моделях): `stream(user_prompt, model, on_delta, system_prompt=None, on_reasoning=None) -> str`; отдельно собирает `delta.reasoning_content|reasoning`; `call(...)` = stream с пустым callback. temperature=0. Префикс модели `opencode-go/` срезается.

### `summary_service.py` (301)
`/summary`, `/sum N`, `/sumc`. Настройки в BotSetting: `summary_instruction` (кастомный системный промпт, команды `/prompt_set|show|reset`), `summary_model` (`/model_set|show|list`; дефолт `SUMMARY_MODEL=opencode-go/qwen3.5-plus`, список `AI_AVAILABLE_MODELS`). Токен-бюджет: `MAX_INPUT_TOKENS=12000`, `MAX_CHARS_PER_MESSAGE=800`, CHARS_PER_TOKEN=4; `_fit_messages_to_token_budget` идёт с конца (свежие важнее). Промпты — файлы через `common.prompts.load(name)`.

### `ask_service.py` (383) — RAG по истории чата (`/ask`)
Pipeline: (1) LLM переписывает вопрос в 3 перефразировки; (2) embed всех вариантов батчем через NLP-сервис `POST /embed/batch`; (3) векторный поиск pgvector `MessageEmbedding.embedding.cosine_distance` по top-K на вариант; (4) **гибрид**: лексический ILIKE-поиск по «корням» слов (`_term_root` — грубый стемминг срезанием окончаний) + скоуп по `@username` из вопроса (similarity 0.97 — чтобы merge не срезал); (5) merge по max similarity → top-25; (6) `_expand_with_neighbors` ±2 соседних сообщения вокруг каждого хита; (7) LLM-ответ со стримом, контекст с маркерами `★`/`·` и MSK-таймштампами. ENV: `ASK_TOP_K=25`, `ASK_NEIGHBORS_EACH_SIDE=2`, `ASK_REWRITE_VARIANTS=3`, `ASK_PER_QUERY_K=15`.

### `digest_service.py` (751) — `/digest N` и еженедельный дайджест (пн 09:00)
Не «жмём всё в LLM», а препроцессинг: burst-detection по часам (медиана×2.5, `BURST_MIN_HOUR_COUNT=8`), внутренние пики по 10-мин слотам, reply-цепочки, характерные униграммы/биграммы burst-vs-background, кандидаты цитат по реакциям, сэмплы. Всё форматируется в структурный промпт. `find_active_chat_ids(window_days=14)`, `has_data_for_period`, `stream_digest`, `generate_digest`.

### `topics_service.py` (205) — `/topics N`
Эмбеддинги сообщений (≥20 симв., ≤1500 с равномерным прореживанием) → KMeans (k=8, `MIN_CLUSTER_SIZE=4`) → ближайшие к центроиду примеры → LLM даёт названия кластеров построчным форматом `CLUSTER n: тема`.

### `user_card_service.py` (254) — `/card [@user|reply]`
Статистика (кол-во, период, средняя длина, топ-5 полученных/поставленных реакций) + сэмпл 200 сообщений в токен-бюджет (70%) → LLM-портрет участника. `resolve_user_for_card`: приоритет reply > @username > автор команды.

### `mood_service.py` (231) — `/mood N`, `/toxic N` — отчёты по `sentiment_label`/`toxicity_score` (проставляет `nlp_classifier`).
### `joke_service.py` (70) — `/joke`, анекдот дня, кэш в BotSetting `joke:{UTC-date}`.
### `phrase_service.py` (119) — `/phrase`, фраза дня в стиле чата: контекст = 80 сообщений за 24ч (≥15 симв.), кэш `phrase:{chat_id}:{date}`.

---

## 14. Пайплайн сообщений и NLP

### `message_service.py` (89)
`save_message(tg_message)` — upsert `User` по `tg_id` + insert `Message` (`message_id`, `telegram_message_id`, `chat_id`, `user_id`, `text`, `emojis` (извлекаются либой `emoji`), `sticker file_id`, `media file_id`, `reply_to`, `created_at`, `edited_at`). `save_reaction(event)` — insert `Reaction(message_id, user_id, emoji)`. Вызывается catch-all хендлером `bot/handlers/messages.py` (сбор 100% сообщений) и `reactions.py`.

### `nlp_classifier.py` (91)
Фоновый батч каждые 30с: сообщения с `nlp_processed_at IS NULL` → `POST {NLP_SERVICE_URL}/classify/batch {"texts": [...]}` → update `sentiment_label/sentiment_score/toxicity_score/nlp_processed_at`. `NLP_WORKER_BATCH=200`.

### `embed_worker.py` (90)
Каждые 45с: сообщения ≥10 симв. без записи в `message_embeddings` → `POST /embed/batch` → `session.merge(MessageEmbedding(message_id, chat_id, embedding, created_at))`. `EMBED_WORKER_BATCH=100`.

### `backfill_runner.py` (97)
Ручной прогон истории из бота (`/backfill`): реестр `JOB_REGISTRY={"embed": embed_pending_once, "nlp": classify_pending_once}`; `BackfillJob` с метриками (processed, rate_per_sec, last_error), цикл до 3 холостых итераций; `start_job/stop_job/all_jobs`.

---

## 15. Планировщик — `scheduler.py` (153)

`start_scheduler(bot) -> AsyncIOScheduler` (APScheduler, TZ Europe/Moscow, все jobs с `coalesce=True, max_instances=1`):

| job id | триггер | функция |
|---|---|---|
| weekly_digest | cron пн 09:00 MSK | `_weekly_digest_job` (активные чаты за 14д, чанки по 3900 симв.) |
| nlp_classify_pending | interval 30s | `classify_pending_once` |
| embed_pending | interval 45s | `embed_pending_once` |
| daily_nominations | cron 10:00 MSK | `run_daily_nominations(bot)` |
| external_markets_check | interval 30m | `auto_resolve_external` |
| markets_auto_close | interval 5m | `auto_close_expired` |
| tag_rentals_expire | interval 5m | `expire_due_sync` |
| market_recover_tick | interval `MARKET_TICK_MIN` (10m) | `recover_and_snapshot_all` (AMM mean-reversion + снапшот цены) |
| nomtag_expire | cron 00:05 MSK | `expire_nomination_tags(bot)` |

Модульная ссылка `_scheduler` + `get_scheduler()` — для отображения jobs в `/admin_status`.

---

## 16. Админ/сервисные

- `admin_service.py` (19): `get_admin_ids()` из `BOT_ADMIN_IDS` (csv), `is_admin_tg_id(tg_id)`.
- `admin_status_service.py` (214): `/admin_status` — health-чеки Postgres (`SELECT 1`), NLP (`/health`), OpenCode (`/models`) с latency; покрытие NLP/эмбеддингов по чатам (raw SQL по `messages`+`message_embeddings`); размеры таблиц из `pg_class`; jobs планировщика; текущая модель; uptime/BUILD_SHA.
- `analytics_service.py` (84): таблица `app_events` (`user_id, chat_id, event view|action, props JSONB, ts`) — usage-аналитика Mini App; `record_event` (best-effort, санитайзинг props: ≤20 ключей, строки ≤200), `summary(hours=24)` — топ роутов/действий.

---

## 17. Маппинг «кто использует сервисы»

**Бот-команды** (bot/handlers): `/summary,/sum,/sumc,/prompt_*,/model_*,/fag,/balance` → messages.py; `/ask`→ask; `/digest`→digest; `/mood,/toxic`→mood; `/topics`→topics; `/card`→user_card; `/joke,/phrase`→joke; `/casino`→deep-link Mini App; `/mystats,/chatstats,/who,/peakday,/streak,/help`→statistic.py (свои запросы, без сервисов); `/admin_status,/backfill`→admin_status+backfill_runner; `/fb`→feedback_admin; `/farmwipe`→farm_admin; catch-all→message_service; media-ссылки→media_dl.

**API Mini App** (api/routes → сервисы): economy→economy_service; markets→markets_service+external_markets; portfolio→markets_service; games→casino_service; clicker→clicker_service+market_service; gacha→gacha_service; duel→duel_service; tags→tag_rental_service; social→social_service; feedback→feedback_service+feedback_ai_service; analytics→analytics_service; admin→economy+markets+feedback+analytics+social.

---

## 18. Чего в эталоне НЕТ (не искать, проектировать заново)

1. **Telegram Stars** — нет ни XTR, ни invoice/pre_checkout. Паттерн идемпотентности платежей заимствовать из `casino_service` (`idem_key` + unique idx + IntegrityError-ретрай) и `nominations_service` (`ref_id` в `economy_tx`).
2. **«Двойник дня» (twin)** — фичи нет нигде в репозитории (в `api/routes/stats.py` только `BiggestWin`). Каркас для реализации — `daily_pick_service` (идемпотентный выбор за MSK-день) + эмбеддинги из `message_embeddings` (для поиска «похожих» участников).
3. **Мут проигравшего дуэли** — нет; дуэль заканчивается только деньгами.
4. **VPN** — в services отсутствует полностью (в новом проекте исключается по ТЗ).

## 19. Топ паттернов для заимствования в Yuvi Bot v2

1. Двойная запись + append-only `economy_tx` с `kind`/`ref_id` — вся экономика аудируема; идемпотентность наград через `ref_id`.
2. `_settle_sync` казино: единая точка «ставка→банк, банк→выплата, журнал, идемпотентность».
3. AMM constant-product с mean-reversion к якорю вместо фикс. курса и дневных кэпов cp→валюта.
4. Гача: pity в строке фермы, x10-гарант, дубли→звёзды→refund, rate-up баннер в BotSetting.
5. Оффлайн-накопление фермы: `min(elapsed, cap)` при каждом обращении, никаких фоновых тиков на юзера.
6. Серверный анти-чит тапов: `min(count, MAX_CPS*elapsed_ms/1000)`.
7. custom_title = promote с минимальным правом + setChatAdministratorCustomTitle; demote = все права False; слоты в BotSetting `nomtag:*` с датой.
8. Redis pub/sub `bal:{chat_id}` ПОСЛЕ commit → SSE в Mini App.
9. Побочные эффекты (Bot API) строго после commit; raw urllib для сервисов, живущих в API-контейнере.
10. LLM только через стрим + строгий JSON с деградацией (feedback_ai) + грунтинг фич продукта в системном промпте.
