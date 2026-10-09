# IOSForge

Конвейер автоматического клонирования приложений. По ссылке на iOS-приложение IOSForge
снимает живое приложение (Frida-архив: UIKit-дерево, шрифты, медиа, сеть), Claude описывает
каждый экран в контракт-спеке, затем локальный Claude Code генерирует **нативное SwiftUI-
приложение** (Xcode-проект через XcodeGen), Vision Judge сверяет каждый экран на **iOS
Simulator** с оригиналом и доводит соответствие, а доставка собирает архив и IPA через
`xcodebuild` и готовит релиз: иконка, стор-скриншоты, legal-страницы, листинг и выгрузка в
**App Store Connect** (за флагом).

Источник правды по требованиям — [`SPEC.md`](./SPEC.md).

## Пайплайн (стадии)

1. **walkthrough** — приём Frida-архива App Store-приложения (`frida_ingest`, контракт —
   `docs/frida-archive.md`); legacy-путь — обход APK на Android-эмуляторе.
2. **analysis** — Claude описывает каждый экран в `app_spec.json` по контракту v3
   (JSON Schema, EARS-требования, provenance; `iosforge/mvp/spec_contract.py`), детерминированно
   уникализирует дизайн (`design.py`).
3. **scope** (опционально, `PIPELINE_SCOPE_GATE`) — предложение MVP-скоупа + iOS-feasibility,
   оператор одобряет экраны в админке.
4. **codegen** (Mac-воркер, очередь `xcode`) — `swiftui_build.build_swiftui`: детерминированный
   scaffold (навигация, вкладки, headless `-screen-id`, разрешения, подписки) → тема →
   компоненты → экраны параллельно, каждый в своей песочнице (`swiftui_gen`), compile gate
   (`xcodebuild`). Затем Vision Judge на Simulator (`compliance.refine_ios_until_complete`,
   стабильные кадры, локаль оригинала) с исправлениями экранов.
5. **delivery** (Mac-воркер) — `swiftui_tasks.run_xcode_delivery`: Apphud + Tenjin через SwiftPM,
   AppIcon, Info.plist (encryption, ATT, SKAdNetwork), `xcodebuild archive` + `exportArchive` →
   подписанный IPA (без team id и ASC-ключа — unsigned IPA). Загрузка в App Store Connect
   (`altool`) только при `IOS_DELIVERY_UPLOAD=true`.
6. **store** — иконка (`app_icon`), стор-скриншоты и iPad-слайды (`store_assets` /
   `slide_contract` / `image_slides`), legal-страницы на GitHub Pages (`legal_pages`), листинг
   (`store_listing`), метаданные ASC (`asc_api` / `asc_credentials`).

Доработки после MVP — раунды оператора (`swiftui_rework.rework_swiftui`): правки по
инструкции и расширение скоупа экранами из полной спеки; каждый раунд — новая версия и
повторная доставка.

## Стек

- **Python 3.12**, окружение и зависимости — **uv** (`pyproject.toml` / `uv.lock`).
- **FastAPI** + **Pydantic v2** — HTTP/API и admin-backend.
- **Celery** + **Redis** — очередь задач по стадиям (`discovery` / `codegen` / `delivery`, плюс
  `xcode` для Mac-воркера), ретраи, dead-letter.
- **PostgreSQL 16** (SQLAlchemy 2 + Alembic) — Job/состояние/таймлайн/аудит.
- **MinIO** (S3-совместимое) — хранилище артефактов с версионированием объектов.
- Админка — **FastAPI + Jinja2 + HTMX** (server-rendered), граф screen map — Cytoscape.js.
- **structlog** — JSON-логи с `job_id` / `stage` контекстом.
- Claude Code worker — отдельный Celery-consumer на хосте (`subprocess` → локальный `claude` CLI).
- **Xcode + XcodeGen + iOS Simulator** — на Mac-воркере (codegen, Vision Judge, архив/IPA).
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
                     SwiftUI codegen, Vision Judge (iOS Simulator), доставка (xcodebuild),
                     иконки, стор-ассеты, legal, ASC
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

Пайплайн реализован сквозняком. Оркестрация — Celery-таски: `iosforge/worker/run_job.py`
(`run_job`, `scope_gate`, `generate_app_icon`, `generate_store_assets` / `generate_ipad_slides`,
`publish_legal_pages`, `generate_store_listing` / `push_store_listing`,
`autofill_store_metadata`, …) и Mac-таски `swiftui_build.build_swiftui`,
`swiftui_tasks.run_xcode_delivery`, `swiftui_rework.rework_swiftui` (очередь `xcode`).

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
uv run celery -A iosforge.common.queue worker -Q codegen   --concurrency=1   # анализ/scope/стор-задачи
uv run celery -A iosforge.common.queue worker -Q delivery  --concurrency=1   # зарезервирована
# Только на Mac (Xcode, XcodeGen, iOS Simulator): SwiftUI-сборка, Vision Judge, архив/IPA, rework
uv run celery -A iosforge.common.queue worker -Q xcode     --concurrency=1
```

Очередь `xcode` Linux-воркеры не слушают. Как Mac-воркер подключается к брокеру прод-сервера
(туннель / сетевой доступ к Redis, Postgres, MinIO) — решение по деплою, не код. Для стор-
слайдов нужен headless Chromium/Chrome (`CHROMIUM_BIN` или PATH); Android-эмулятор нужен
только для legacy-пути APK.

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
# Воркер стадии (анализ/scope/стор); SwiftUI-сборка — отдельный Mac-воркер (-Q xcode):
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
Replicate/GitHub) берутся только из окружения или защищённого хранилища
(`secrets/`, gitignored) и в репозиторий не коммитятся.

## Документация

- Локальная инфраструктура (docker-compose, порты, проверки) — [`docs/local-infra.md`](./docs/local-infra.md).
- Админка — [`docs/admin.md`](./docs/admin.md).
- SwiftUI-вертикаль (захват → нативное приложение → Vision Judge) — [`docs/mvp-vertical.md`](./docs/mvp-vertical.md).
- Эталонный SwiftUI-проект контракта — [`docs/swiftui-reference/`](./docs/swiftui-reference/).
- Контракт описания приложения `app_spec` — [`docs/app-spec-v2.md`](./docs/app-spec-v2.md)
  (в коде контракт v3 — `iosforge/mvp/spec_contract.py`).
- Нативная доставка: архив, IPA, подпись, загрузка в ASC — [`docs/store-upload.md`](./docs/store-upload.md).
- Дефекты выпущенных приложений (накопитель правок) — [`docs/app-defects.md`](./docs/app-defects.md).
- Контракт Frida-архива обхода — [`docs/frida-archive.md`](./docs/frida-archive.md).
- Требования — [`SPEC.md`](./SPEC.md).
