# IOSForge

Конвейер автоматического клонирования приложений. По ссылке на iOS-приложение IOSForge
находит Android-аналог, скачивает APK, обходит его на эмуляторе (собирая скриншоты и
screen map), затем локальный Claude Code по контракт-спеке генерирует Flutter-приложение
**под iOS**, итеративно доводит соответствие оригиналу до **≥95%** (сверка — рендер
web-сборки в headless Chrome) и готовит релиз: иконка, стор-скриншоты, legal-страницы,
листинг, сборка на CodeMagic и отправка в **App Store Connect**.

Android используется только на **входе** (обход приложения-источника); выход — iOS/Flutter.

Источник правды по требованиям — [`SPEC.md`](./SPEC.md).

## Пайплайн (стадии)

1. **discovery** — поиск Android-аналога исходного iOS-приложения (каталог-провайдер).
2. **acquisition** — скачивание APK (apk-провайдер).
3. **emulator** — обход APK на Android-эмуляторе: скриншоты + screen map (`screens.json`).
   Альтернативный источник обхода — Frida-архив (см. `docs/frida-archive.md`).
4. **analysis_codegen** — Claude описывает каждый экран в `app_spec.json` по контракту v3
   (JSON Schema, EARS-требования, provenance; `iosforge/mvp/spec_contract.py`),
   детерминированно уникализирует дизайн (`design.py`), раскладывает задачи в слои-DAG
   (`topo_layers`) и генерит Flutter: `scaffold → component_library → экраны (параллельно,
   каждый в своём git-worktree)`. Сверка с эталоном — рендер web-сборки в headless
   Chromium (`compliance.py`, `verify_web` / `refine_web_until_complete`), петля до **≥95%**.
5. **delivery** — иконка (`app_icon` + `icon_stage`), стор-скриншоты и iPad-слайды
   (`store_assets` / `slide_contract` / `image_slides`), legal-страницы на GitHub Pages
   (`legal_pages`), листинг (`store_listing`), подписки **Apphud** + атрибуция **Tenjin**,
   сборка на **CodeMagic** (`codemagic_*`) и публикация в **App Store Connect**
   (`asc_api` / `asc_credentials`).

## Стек

- **Python 3.12**, окружение и зависимости — **uv** (`pyproject.toml` / `uv.lock`).
- **FastAPI** + **Pydantic v2** — HTTP/API и admin-backend.
- **Celery** + **Redis** — очередь задач по стадиям (`discovery` / `codegen` / `delivery`),
  ретраи, dead-letter.
- **PostgreSQL 16** (SQLAlchemy 2 + Alembic) — Job/состояние/таймлайн/аудит.
- **MinIO** (S3-совместимое) — хранилище артефактов с версионированием объектов.
- Админка — **FastAPI + Jinja2 + HTMX** (server-rendered), граф screen map — Cytoscape.js.
- **structlog** — JSON-логи с `job_id` / `stage` контекстом.
- Claude Code worker — отдельный Celery-consumer на хосте (`subprocess` → локальный `claude` CLI).
- **Replicate** (seedream) — генерация иконки и фоновых изображений для стор-скринов.
- Качество: **pytest** (тесты), **ruff** (линт/формат), **mypy** (типы, strict на доменном слое).

## Структура

```
iosforge/            пакет приложения
  orchestrator/      машина состояний Job, таймлайн, диспетчер событий
  services/          этапы-таски: discovery / acquisition / emulator / analysis_codegen / delivery
  providers/         сменные провайдеры: catalog / apk / emulator / codegen / promptset + реестр
  worker/            Claude Code worker + Celery-таски пайплайна (run_job.py)
  mvp/               реализованная вертикаль: анализ, контракт app_spec, уникализация,
                     codegen, compliance (web-рендер), иконки, стор-ассеты, legal, ASC/CodeMagic
  admin/             FastAPI-приложение (create_app + /health) + Jinja/HTMX UI
  storage/           абстракция артефактов поверх MinIO/S3
  db/                SQLAlchemy-модели + Alembic-миграции
  common/            config.py (настройки), logging.py (structlog), queue.py (Celery), types.py (enum)
tests/               pytest
configs/             runtime-конфиги без редеплоя (выбор провайдеров, источники, пороги/лимиты)
docs/                runbooks и контракты (см. раздел «Документация»)
artifacts/           вывод пайплайна (gitignored)
docker-compose.yml   локальная инфраструктура: postgres + redis + minio
```

Пайплайн реализован сквозняком: стадии `discovery → … → delivery`, codegen и весь
store-delivery слой. Оркестрация — Celery-таски в `iosforge/worker/run_job.py`
(`run_job`, `build_frontend`, `rework_frontend`, `run_codemagic_build`, `generate_app_icon`,
`generate_store_assets` / `generate_ipad_slides`, `publish_legal_pages`,
`generate_store_listing` / `push_store_listing`, `autofill_store_metadata`, `reverify_web`, …).

## Как запустить

Тулчейн — `uv`. При необходимости добавьте его в PATH: `export PATH="$HOME/.local/bin:$PATH"`.

```bash
# Установка зависимостей (создаёт .venv из pyproject.toml / uv.lock)
uv sync

# Локальная инфраструктура (PostgreSQL + Redis + MinIO). Подробности — docs/local-infra.md
docker compose up -d

# Схема БД (admin_users + Job)
uv run alembic upgrade head

# Dev-сервер admin-backend (health: GET /health)
uv run uvicorn iosforge.admin.app:app --reload

# Тесты / линт / формат / типы
uv run pytest
uv run ruff check .
uv run ruff format
uv run mypy iosforge
```

Воркеры стадий (каждый — на свою очередь):

```bash
uv run celery -A iosforge.common.queue worker -Q discovery --concurrency=1   # discovery/acquisition/emulator
uv run celery -A iosforge.common.queue worker -Q codegen   --concurrency=1   # анализ/codegen/verify/сборка-интеграция
uv run celery -A iosforge.common.queue worker -Q delivery  --concurrency=1   # иконки/стор-ассеты/legal/листинг/публикация
```

Для стадии `emulator` на хосте нужен загруженный Android-эмулятор (обход приложения-источника);
для web-сверки — headless Chromium/Chrome.

## Админка конвейера

Веб-админка (логин, ручная загрузка APK, запуск/отслеживание Job, артефакты, загрузка ключей
подписи ASC). Подробности — [`docs/admin.md`](./docs/admin.md).

```bash
uv sync && docker compose up -d
uv run alembic upgrade head                       # схема (admin_users + Job)
# Завести оператора (без публичной регистрации); пароль ≥ 12 символов, только из env:
ADMIN_SEED_USERNAME=operator ADMIN_SEED_PASSWORD='<сильный-пароль>' \
  uv run python -m iosforge.admin.seed
# Приложение слушает ТОЛЬКО loopback; наружу — через reverse-proxy (см. ниже):
uv run uvicorn iosforge.admin.app:app --host 127.0.0.1 --port 8070 --proxy-headers
# Воркер стадии (нужен загруженный Android-эмулятор на хосте для обхода источника):
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
[`.env.example`](./.env.example). Секреты (ключи S3/MinIO, ASC-ключи подписи, токены
Replicate/CodeMagic/GitHub) берутся только из окружения или защищённого хранилища
(`secrets/`, gitignored) и в репозиторий не коммитятся.

## Документация

- Локальная инфраструктура (docker-compose, порты, проверки) — [`docs/local-infra.md`](./docs/local-infra.md).
- Админка — [`docs/admin.md`](./docs/admin.md).
- MVP-вертикаль (сквозной путь обхода → Flutter) — [`docs/mvp-vertical.md`](./docs/mvp-vertical.md).
- Контракт описания приложения `app_spec` — [`docs/app-spec-v2.md`](./docs/app-spec-v2.md)
  (в коде контракт v3 — `iosforge/mvp/spec_contract.py`).
- Заливка iOS-билда в App Store (CodeMagic + ASC) — [`docs/store-upload.md`](./docs/store-upload.md).
- Дефекты выпущенных приложений (накопитель правок) — [`docs/app-defects.md`](./docs/app-defects.md).
- Контракт Frida-архива обхода — [`docs/frida-archive.md`](./docs/frida-archive.md).
- Требования — [`SPEC.md`](./SPEC.md).
