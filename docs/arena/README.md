# Yuvi Arena

Документация игрового режима Arena для `YUVI-BOT-v2`.

## Документы

- [`FIGHTING_SPEC.md`](./FIGHTING_SPEC.md) — целевое техническое задание: правила боя, экономика, UX, безопасность и Definition of Done.
- [`FIGHTING_ROADMAP.md`](./FIGHTING_ROADMAP.md) — поэтапный план разработки и фактическая карта текущей реализации.

## Текущий реализованный срез

На 2026-08-08 в репозитории доступны:

- плитка **«Арена»** на Mini App hub;
- маршрут Mini App `/arena`;
- выбор 4 бойцов;
- создание матча со ставкой от 100 ювиков;
- список открытых матчей;
- принятие матча другим игроком;
- подтверждение участия;
- отмена собственного ожидающего матча;
- восстановление собственных незавершённых матчей;
- периодическое обновление lobby;
- серверная авторизация и скрытие бойца соперника до начала боя;
- боевой runtime с REST/SSE, reconnect, disconnect/forfeit и серверным terminal-state;
- settlement: выплата/возврат, рейтинг, XP, публикация баланса и admin refund с аудитом;
- read-only admin hardening: overview состояния матчей и баланса Arena fund, paginated fund ledger;
- страницы результата, истории, лидерборда, профиля и базовой тренировки.

Основные файлы реализации:

- `miniapp/src/routes/+page.svelte` — плитка Arena;
- `miniapp/src/routes/arena/+page.svelte` — lobby Mini App;
- `api/routes/arena.py` — lobby API;
- `api/routes/arena_runtime.py` — runtime API активного матча;
- `bot/services/arena_service.py` — lifecycle и финансовая логика lobby;
- `bot/services/arena_session_service.py` — серверное состояние боевой сессии;
- `common/arena/` — конфигурация, бойцы, enum и движок;
- `tests/arena/` — тесты контрактов, движка, lobby, runtime и безопасности.

## Следующий вертикальный срез

Закрыть post-MVP эксплуатационные функции:

1. ежедневный/недельный scheduler наград и snapshots;
2. дайджест Arena и server-side MP4 replay;
3. расширенная админка: overview, фонды и ledger уже реализованы; force-finish сознательно оставлен отдельным безопасностным решением;
4. rate limits уже реализованы; остаются structured metrics и PostgreSQL integration/load smoke tests;
5. полноценные 2.5D-ассеты, аудио и настройки reduced motion/mute.

Базовый PvP-путь от lobby до результата уже реализован; перед продакшеном остаётся пройти PostgreSQL-среду и ручной Telegram WebView smoke test.
