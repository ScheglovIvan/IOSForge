# IOSForge

Конвейер автоматического клонирования приложений. По ссылке на iOS-приложение IOSForge
находит Android-аналог, скачивает APK, обходит его на эмуляторе (собирая скриншоты и
screen map), после чего локальный Claude Code генерирует Flutter-приложение под iOS и
итеративно доводит его до соответствия оригиналу ≥95%.

Источник правды по требованиям — [`SPEC.md`](./SPEC.md).

## Стек

Вариант A (моно-язык на Python, быстрый старт):

- **Python 3.12**, окружение и зависимости — **uv** (`pyproject.toml` / `uv.lock`).
- **FastAPI** + **Pydantic v2** — HTTP/API и admin-backend.
- **Celery** + **Redis** — очередь задач по стадиям пайплайна, ретраи, маршрутизация.
- **PostgreSQL 16** (SQLAlchemy 2 + Alembic) — Job/состояние/таймлайн/аудит.
- **MinIO** (S3-совместимое) — хранилище артефактов с версионированием объектов.
- Админка — **FastAPI + Jinja2 + HTMX** (server-rendered), граф screen map — Cytoscape.js.
- Claude Code worker — отдельный Celery-consumer на хосте (`subprocess` → локальный `claude` CLI).
- Качество: **pytest** (тесты), **ruff** (линт/формат), **mypy** (типы, strict на доменном слое).

## Структура

```
iosforge/            пакет приложения
  orchestrator/      машина состояний Job, таймлайн, диспетчер событий
  services/          этапы-таски: discovery / acquisition / emulator / analysis_codegen / delivery
  providers/         сменные провайдеры: catalog / apk / emulator / codegen / promptset + реестр
  worker/            Claude Code worker (subprocess-обвязка над claude CLI)
  admin/             FastAPI-приложение (create_app + /health), далее Jinja/HTMX UI
  storage/           абстракция артефактов поверх MinIO/S3
  db/                SQLAlchemy-модели + Alembic-миграции
  common/            общий слой: config.py (настройки), logging.py (structlog), types.py (domain-enum)
tests/               pytest
configs/             runtime-конфиги без редеплоя (выбор провайдеров, источники, пороги/лимиты)
docs/                runbooks (см. docs/local-infra.md)
artifacts/           вывод пайплайна (gitignored)
docker-compose.yml   локальная инфраструктура: postgres + redis + minio
```

Большинство пакетов под `iosforge/` пока заготовки под последующие эпики. Реализованы:
admin health-эндпоинт и общий слой `iosforge/common/` (config / logging / types).

## Как запустить

Тулчейн — `uv`. При необходимости добавьте его в PATH: `export PATH="$HOME/.local/bin:$PATH"`.

```bash
# Установка зависимостей (создаёт .venv из pyproject.toml / uv.lock)
uv sync

# Локальная инфраструктура (PostgreSQL + Redis + MinIO). Подробности — docs/local-infra.md
docker compose up -d

# Dev-сервер admin-backend (health: GET /health)
uv run uvicorn iosforge.admin.app:app --reload

# Тесты / линт / формат / типы
uv run pytest
uv run ruff check .
uv run ruff format
uv run mypy iosforge
```

Воркеры Celery (`uv run celery -A iosforge.common.queue worker -Q <queue>`) появятся
с реализацией оркестратора в следующих эпиках.

## Админка конвейера

Веб-админка (логин, ручная загрузка APK, отслеживание Job, артефакты). Подробности —
[`docs/admin.md`](./docs/admin.md).

```bash
uv sync && docker compose up -d
uv run alembic upgrade head                       # схема (admin_users + Job)
# Завести оператора (без публичной регистрации); пароль ≥ 12 символов, только из env:
ADMIN_SEED_USERNAME=operator ADMIN_SEED_PASSWORD='<сильный-пароль>' \
  uv run python -m iosforge.admin.seed
# Приложение слушает ТОЛЬКО loopback; наружу — через reverse-proxy (см. ниже):
uv run uvicorn iosforge.admin.app:app --host 127.0.0.1 --port 8070 --proxy-headers
# Воркер, прогоняющий Job (нужен загруженный Android-эмулятор на хосте):
uv run celery -A iosforge.common.queue worker -Q codegen --concurrency=1
```

### Деплой: приложение за HTTPS reverse-proxy (обязательно)

Порт приложения **не открывается в интернет** — слушает `127.0.0.1:8070`. Наружу торчит
только `443` у прокси с HTTPS (иначе пароль/сессия летят открытым текстом). Прод: задать
`ADMIN_SESSION_SECRET`, `ADMIN_COOKIE_SECURE=true` (по умолчанию), сильные seed-креды.

**Caddy** (`Caddyfile`) — автоматический TLS:
```
admin.example.com {
    encode gzip
    request_body { max_size 210MB }      # ≥ ADMIN_UPLOAD_MAX_BYTES (200 MiB)
    # опц. второй слой: basic_auth перед приложением
    # basic_auth { operator JDJhJD... }
    reverse_proxy 127.0.0.1:8070 { header_up X-Forwarded-Proto {scheme} }
}
```

**nginx** (`/etc/nginx/sites-enabled/admin`):
```nginx
server {
    listen 443 ssl;
    server_name admin.example.com;
    ssl_certificate     /etc/letsencrypt/live/admin.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/admin.example.com/privkey.pem;
    add_header Strict-Transport-Security "max-age=31536000; includeSubDomains" always;
    client_max_body_size 210m;            # ≥ лимита загрузки APK в приложении
    location / {
        proxy_pass http://127.0.0.1:8070;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        # опц. второй слой: auth_basic "IOSForge"; auth_basic_user_file /etc/nginx/.htpasswd;
    }
}
server { listen 80; server_name admin.example.com; return 301 https://$host$request_uri; }
```

**Firewall:** наружу открыт только `443` (и `22` для SSH); порт приложения (`8070`) —
закрыт/слушает только loopback. Пример: `ufw allow 443/tcp && ufw allow 22/tcp && ufw enable`.

## Конфигурация

Настройки читаются из переменных окружения, опционального `.env` и `configs/app.toml`
(приоритет: env > `.env` > TOML-конфиг > значения по умолчанию). Шаблон переменных —
[`.env.example`](./.env.example). Секреты (ключи S3/MinIO, учётные данные подписи) берутся
только из окружения или защищённого хранилища и в репозиторий не коммитятся.

## Документация

- Локальная инфраструктура (docker-compose, порты, проверки) — [`docs/local-infra.md`](./docs/local-infra.md).
- Требования — [`SPEC.md`](./SPEC.md).
