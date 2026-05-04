[![CI](https://github.com/wancheez/wanhub/actions/workflows/ci.yml/badge.svg)](https://github.com/wancheez/wanhub/actions/workflows/ci.yml)
[![codecov](https://codecov.io/gh/wancheez/wanhub/branch/main/graph/badge.svg)](https://codecov.io/gh/wancheez/wanhub)

# wanhub

FastAPI-сервер + Telegram-бот для Raspberry Pi 5 с интеграцией Claude API.

Хобби-проект: личная страничка «О себе», мониторинг состояния Pi, генерация ASCII-арта и чат с Claude — всё в одном процессе.

## Фичи

### HTTP API / web

- `GET /` — страница «О себе» (профиль из `app/services/profile.py`)
- `GET /device` — состояние Pi: температура CPU, частота, нагрузка, память, диск (с автообновлением каждые 2 секунды)
- `GET /api/device` — JSON с теми же метриками
- `POST /api/ascii` — случайный ASCII-арт от Claude (Haiku 4.5)
- `GET /ping` — health check
- `GET /docs` — автодокументация Swagger (FastAPI)

### Telegram-бот (опционален — включается через `.env`)

Триггер: сообщение должно начинаться со слова **«Чат»** (case-insensitive). Команды-слэши работают всегда.

- **`Чат, <вопрос>`** — диалог с Claude. История чата хранится в SQLite, переживает рестарты.
- **`Чат, <вопрос>` в реплае** — если триггер отправлен ответом на чьё-то сообщение, цитируемый текст подмешивается в запрос как контекст (`> цитата` + автор). Цитата обрезается до 1000 символов.
- **`Чат, пришли фото кота`** — поиск картинки в DuckDuckGo + отправка. Запрос рерайтится через Haiku в нормальную поисковую фразу.
- **`/device`** — состояние Pi
- **`/ascii`** — случайный ASCII-арт
- **`/reset`** — сбросить историю чата
- **`/whoami`** — твой `chat_id`
- **`/start`, `/help`** — справка

Доступ ограничен whitelist-ом `chat_id` в `.env`. Чужие сообщения молча игнорируются.

## Архитектура

```
app/
├── main.py              # FastAPI factory + lifespan для бота
├── __main__.py          # python -m app
├── api/routes/          # HTTP endpoints
├── bot/                 # Telegram bot (aiogram)
│   ├── auth.py          # ChatWhitelistMiddleware
│   ├── format.py        # Markdown → Telegram HTML конвертер
│   ├── handlers/        # /start, /device, /ascii, чат
│   └── skills/          # regex-skill слой (intent-match → действие до LLM)
├── services/            # бизнес-логика (chat, ascii, device, image_search)
├── prompts/             # системные промты для Claude (.md файлы)
├── core/                # config, logging
├── schemas/             # Pydantic-модели
├── templates/           # Jinja2 HTML
└── static/              # CSS/JS
```

Polling, не webhook — Pi сам ходит в `api.telegram.org`. Это надёжнее с серым IP / роутерами с фильтрами Telegram-сетей.

## Quick start

```bash
git clone https://github.com/wancheez/wanhub.git
cd wanhub

# 1. Зависимости (нужен Python 3.13+, Poetry)
poetry install

# 2. Конфиг
cp .env.example .env
# отредактировать .env: ANTHROPIC_API_KEY обязателен,
# TELEGRAM_BOT_TOKEN — для бота (необязательно)

# 3. Запуск (dev — с автоперезагрузкой при правках)
make dev
# или прод-режим
make run
```

Сервер слушает `http://0.0.0.0:8000/`. Для запуска бота нужно в `.env` положить `TELEGRAM_BOT_TOKEN` от [@BotFather](https://t.me/BotFather) и `TELEGRAM_ALLOWED_CHAT_IDS` — твой `chat_id` (увидишь в `logs/app.log` строкой `blocked telegram chat_id=N` после первого сообщения боту).

## Make targets

| Цель | Что делает |
|---|---|
| `make install` | Установить зависимости через poetry |
| `make run` | Запустить сервер (без reload) |
| `make dev` | Запустить с auto-reload |
| `make test` | pytest |
| `make lint` | ruff check + ruff format --check |
| `make format` | ruff format |
| `make fix` | ruff check --fix + ruff format |
| `make typecheck` | mypy |
| `make check` | lint + typecheck + test (pre-commit gate) |
| `make service-install` | Установить как systemd-сервис (автозапуск при загрузке) |
| `make service-restart` | Рестарт systemd-сервиса |
| `make service-logs` | journalctl -f |

## Запуск как systemd-сервис

```bash
make service-install
```
Скрипт подставит твоего юзера и путь в template, установит юнит в `/etc/systemd/system/wanhub.service`, включит автозапуск, стартанёт. После этого сервер сам поднимается при загрузке Pi.

## Стек

- Python 3.13
- [FastAPI](https://fastapi.tiangolo.com/) + [Uvicorn](https://www.uvicorn.org/)
- [aiogram 3.x](https://aiogram.dev/) — Telegram bot
- [Anthropic SDK](https://github.com/anthropics/anthropic-sdk-python) — Claude API
- [ddgs](https://pypi.org/project/ddgs/) — DuckDuckGo image search
- [Pydantic 2](https://docs.pydantic.dev/) — модели/валидация
- SQLite — история чата
- [ruff](https://docs.astral.sh/ruff/) + [mypy](https://mypy.readthedocs.io/) — линтер + типчек
- [Poetry](https://python-poetry.org/) — управление зависимостями

## Стоимость Anthropic API

Бот использует `claude-haiku-4-5` (~$1/$5 за 1M токенов вход/выход). Реалистичные оценки:
- Чат — ~$0.0001 за обмен сообщениями
- ASCII-арт — ~$0.001 за картинку
- Image-rewrite — ~$0.0001 за поиск

На $5 хватит примерно на 5000+ взаимодействий. Поставь spending limit в [Anthropic Console](https://console.anthropic.com/settings/limits) перед публикацией.

## Лицензия

[MIT](LICENSE) — Иван Ерохин, 2026.

## Зачем сделано

Учебный проект, чтобы поковырять FastAPI/aiogram/Claude API на железе Raspberry Pi 5 в формате «всё своё под одним процессом».
