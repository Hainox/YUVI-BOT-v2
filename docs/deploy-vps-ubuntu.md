# Деплой Yuvi Bot v2 на VPS Ubuntu

## 1. Подготовка сервера

```bash
sudo apt update
sudo apt install -y docker.io docker-compose-plugin git
sudo usermod -aG docker $USER
```

Перезайди в SSH после добавления в группу docker.

## 2. Клонирование и env

```bash
git clone <your-repo-url> yuvi-bot-v2
cd yuvi-bot-v2
cp .env.example .env
```

Заполни `.env` (минимум: `BOT_TOKEN`, `DATABASE_URL`, `REDIS_URL`).

## 3. Запуск

```bash
docker compose up -d --build
docker compose ps
```

## 4. Проверка

```bash
curl http://localhost:8002/health
curl http://localhost:8001/health
```

В Telegram отправь `/health` боту.

## 5. HTTPS для Mini App (Let's Encrypt, автоматически)

Без домена Mini App и так работает — по обычному HTTP на порту 8003 (см. шаги
выше). Как только у вас появится домен, HTTPS включается двумя строками в
`.env` — сертификат Let's Encrypt получается и продлевается автоматически,
никаких ручных команд certbot запускать не нужно.

### 5.1 Перед включением

1. **Купите домен** и настройте DNS A-запись на IP этого VPS. Проверьте, что
   домен уже резолвится на сервер (`ping example.com` — должен вернуть IP
   вашего VPS), иначе Let's Encrypt не сможет подтвердить владение доменом
   (HTTP-01 challenge требует, чтобы домен уже указывал на сервер).
2. **Откройте порты 80 и 443** на VPS (в панели провайдера и/или `ufw`):
   ```bash
   sudo ufw allow 80/tcp
   sudo ufw allow 443/tcp
   ```

### 5.2 Включение

Впишите в `.env` (замените `example.com` на свой реальный домен):

```
DOMAIN=example.com
CERTBOT_EMAIL=you@example.com
COMPOSE_PROFILES=https
```

Затем как обычно:

```bash
docker compose up -d --build
docker compose logs -f nginx-https
```

Сервис `nginx-https` сам получит сертификат и начнёт принимать HTTPS-трафик
на портах 80/443, проксируя на неизменённые `miniapp`/`api` контейнеры.

### 5.3 Проверка на STAGING перед боевым выпуском

По умолчанию `STAGING=1` — сертификаты выпускаются в тестовом (staging)
окружении Let's Encrypt, у которого гораздо более высокий лимит запросов.
Это защищает от блокировки боевого лимита, если что-то пойдёт не так при
первой настройке. Браузер будет показывать предупреждение "сертификат не
доверенный" — это ожидаемо для staging.

Когда убедитесь, что `docker compose logs nginx-https` не показывает ошибок
и HTTPS реально работает — переключитесь на боевой выпуск:

```
STAGING=0
```

и перезапустите `docker compose up -d nginx-https`.

### 5.4 Если домена ещё нет

HTTPS — не обязательное условие запуска стека. Без `DOMAIN`/`COMPOSE_PROFILES`
в `.env` сервис `nginx-https` не поднимается вовсе (порты 80/443 не заняты),
а Mini App продолжает работать по HTTP на 8003, как в разделах 1-4 выше.
Живая проверка "Mini App открывается по HTTPS" откладывается до момента, пока
у вас не появится реальный домен — тогда просто выполните шаги 5.1-5.3.

### 5.5 Секреты в проде

Вынести секреты в безопасное хранилище (например, не хранить прод-`.env` в
общедоступных бэкапах) — рекомендуется как общая практика, отдельного
механизма в проекте под это не заведено.

