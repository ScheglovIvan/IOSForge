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
  сгенерированный `flutter_app.zip`. Доступ к объекту проверяет принадлежность префиксу `jobs/<id>/`.

## Безопасность (порт смотрит в интернет)
- Пароли — **argon2** (`argon2-cffi`), только хеш в БД, плейнтекст не логируется.
- Сессия — серверная (Redis), cookie `HttpOnly`+`Secure`+`SameSite=Lax`, sliding-таймаут.
- **CSRF**: синхронизатор-токен из сессии на формах + double-submit `csrf_pre` на логине.
- **Rate-limit логина**: счётчик неудач по (username, IP) в Redis, лок после N попыток (429).
- **Security-заголовки**: HSTS, CSP (`default-src 'self'`), `X-Frame-Options: DENY`, `nosniff`,
  `Referrer-Policy: no-referrer`, `Cache-Control: no-store`. Docs/Redoc выключены.
- APK хранится в MinIO (вне веб-рута), веб-слой его не исполняет.

## Воркер
`iosforge/worker/run_job.py` — Celery-таска (`queue=codegen`, `concurrency=1`): скачивает APK из
MinIO → гоняет MVP-вертикаль (эмулятор → обход → Claude Code) → пишет стадии в `StageTimeline`,
скриншоты/`screens.json`/`flutter_app.zip` в MinIO, `WalkthroughResult`/`GenerationResult` в БД,
переводит `Job` в DONE/FAILED. **Требует загруженный Android-эмулятор (AVD `mvp`) на хосте** —
без запущенного воркера Job остаётся `QUEUED` (виден в списке).

## Настройки (env, см. `.env.example`)
`ADMIN_SESSION_SECRET`, `ADMIN_COOKIE_SECURE` (prod=true), `ADMIN_SESSION_TTL_S`,
`ADMIN_UPLOAD_MAX_BYTES`, `ADMIN_LOGIN_MAX_ATTEMPTS`, `ADMIN_LOGIN_LOCKOUT_S`, `ADMIN_AVD`,
`ADMIN_SEED_USERNAME`/`ADMIN_SEED_PASSWORD` (только для сид-команды).

## Вне скоупа MVP (фаза 2)
CRUD промптов/провайдеров/источников, роли сложнее одного оператора, discovery/acquisition
(APK грузим руками), метрика ≥95%, вебсокеты, граф Cytoscape, дизайн.
