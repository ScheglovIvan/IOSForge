# Админка конвейера (MVP)

Веб-интерфейс оператора над уже готовой инфраструктурой (EPIC-2: БД/Celery/MinIO) и
MVP-вертикалью (`iosforge/mvp`). Internet-facing за HTTPS reverse-proxy — деплой в README.

## Что умеет
- **Логин** оператора (логин+пароль), серверная сессия в Redis, без публичной регистрации
  (аккаунты — командой `python -m iosforge.admin.seed`). Любой запрос без сессии → редирект на
  `/login` (браузер) или `401` (API). Открыт только `/health` и страница логина.
- **Загрузка APK** руками → валидация (расширение, реальный zip с `AndroidManifest.xml`, лимит
  размера, санитизация имени) → объект в MinIO + создание `Job(QUEUED)` + enqueue Celery-задачи.
- **Отслеживание**: список Jobs, карточка (этап/таймлайн/статус/ошибки) с автообновлением
  (meta-refresh поллингом, пока Job не терминальный).
- **Артефакты** по Job: галерея скриншотов, карта экранов (`screens.json`), ссылка на
  сгенерированные исходники (`xcode_app.zip`; у legacy-джоб — `flutter_app.zip`). Доступ к объекту проверяет принадлежность префиксу `jobs/<id>/`.

## Безопасность (порт смотрит в интернет)
- Пароли — **argon2** (`argon2-cffi`), только хеш в БД, плейнтекст не логируется.
- Сессия — серверная (Redis), cookie `HttpOnly`+`Secure`+`SameSite=Lax`, sliding-таймаут.
- **CSRF**: синхронизатор-токен из сессии на формах + double-submit `csrf_pre` на логине.
- **Rate-limit логина**: счётчик неудач по (username, IP) в Redis, лок после N попыток (429).
- **Security-заголовки**: HSTS, CSP (`default-src 'self'`), `X-Frame-Options: DENY`, `nosniff`,
  `Referrer-Policy: no-referrer`, `Cache-Control: no-store`. Docs/Redoc выключены.
- APK хранится в MinIO (вне веб-рута), веб-слой его не исполняет.

## Воркер
`iosforge/worker/run_job.py` — Celery-таска (`queue=codegen`, `concurrency=1`): принимает
Frida-архив (legacy: APK + эмулятор), запускает анализ, пишет стадии в `StageTimeline`,
скриншоты/`screens.json`/`app_spec.json` в MinIO и `WalkthroughResult` в БД, затем передаёт
джобу scope-гейту или SwiftUI-сборке. SwiftUI-сборка, Vision Judge, архив/IPA и rework идут
на Mac-воркере очереди `xcode` (`swiftui_build`, `swiftui_tasks`, `swiftui_rework`); без него
джоба остаётся в CODEGEN. Без запущенного воркера `codegen` Job остаётся `QUEUED`.

## Recovering a stuck or failed job

Two operator actions on the job page bring a job back without re-running the expensive analysis.
Both ask for confirmation, need a signed-in operator, and are logged in the job timeline
(muted rows starting with `operator:`) and in the audit log.

### Reset stuck job

Use it when a job sits in an intermediate state (Queued, Walkthrough, Analysis, Scope, Codegen…)
but its worker task is gone (worker restarted, out of memory, killed), or when a native delivery
stays "queued/archiving" forever.

The button appears after 5 minutes without activity (`JOB_RESET_MIN_IDLE_S`; the worker
probe waits `JOB_PROBE_TIMEOUT_S` for an answer). On click the admin
asks every Celery worker and the Redis queues whether a task for this job is still running or
waiting. If it is, the reset is refused (409) with the worker or queue name, so wait or check
`scripts/factory.sh status`. A message left behind by a killed worker does not block the reset.
It is listed in the audit entry and becomes a no-op if Redis redelivers it later. Messages
queued before this feature was deployed carry no run epoch and are not fenced, so right after
the deploy wait out the broker visibility timeout (6 h) before resetting a job whose old task
was killed.

Where the job goes:

| Stuck in | Result |
|---|---|
| Queued / Walkthrough / Analysis | back to Queued, the analysis restarts from the uploaded archive |
| Scope | the scope proposal is prepared again |
| Codegen, app already built (a rework round died) | Done (or Needs input if the last build was held), the last good version stays deliverable |
| Codegen, first build, scope approved | the scope is re-opened (see below), approve it again to rebuild (Failed if the stored scope cannot be read) |
| Codegen, first build, no scope gate | Failed, press **Build app** |
| a stuck native delivery | the delivery is marked failed, the job state is unchanged |

A delivery only runs while its row is *queued*. If its final database write fails, the Celery
retry no longer starts it again, so the delivery stays *archiving* until an operator resets it.

Open stages are closed with "interrupted — reset by <operator>".

### Re-open scope

For a **failed** job whose app was never built (for example the build stopped with
"screens reachable from no tab"). The approved scope goes back to *proposed* with your previous
selection, notes and capability routing kept, and the job waits in **Needs input** on the Scope
tab. Fix the screen set and press **Approve & generate**. The approval is checked again, and the
build starts on the Mac worker.

Kept: the analysis, screenshots, app spec and every earlier scope version (MinIO versioning). The
next build overwrites the generated screenshots and sources. Re-open is refused when the scope
gate is off, the app is already built (use Rework / Extend scope instead), the job failed before
the scope stage, or a retry of the job is still scheduled.

## Настройки (env, см. `.env.example`)
`ADMIN_SESSION_SECRET`, `ADMIN_COOKIE_SECURE` (prod=true), `ADMIN_SESSION_TTL_S`,
`ADMIN_UPLOAD_MAX_BYTES`, `ADMIN_LOGIN_MAX_ATTEMPTS`, `ADMIN_LOGIN_LOCKOUT_S`, `ADMIN_AVD`,
`ADMIN_SEED_USERNAME`/`ADMIN_SEED_PASSWORD` (только для сид-команды), `JOB_RESET_MIN_IDLE_S`,
`JOB_PROBE_TIMEOUT_S` (восстановление застрявших Job).

## Вне скоупа MVP (фаза 2)
CRUD промптов/провайдеров/источников, роли сложнее одного оператора, discovery/acquisition
(APK грузим руками), метрика ≥95%, вебсокеты, граф Cytoscape, дизайн.
