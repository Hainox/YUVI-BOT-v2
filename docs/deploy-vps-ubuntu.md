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

## 5. Следующий шаг для прод

- Поднять nginx как reverse proxy.
- Подключить HTTPS (Let's Encrypt).
- Вынести секреты в безопасное хранилище.

