# Runbook: нативная доставка iOS-билда в App Store Connect

Как довести сгенерированное SwiftUI-приложение до подписанного IPA и билда в App Store
Connect. Сборка идёт на Mac-воркере через `xcodebuild` (автоподпись Xcode по ASC API-ключу) —
руками остаются только действия в аккаунтах Apple.

## Важно: у каждого приложения свой Apple-аккаунт

**У каждого клонируемого приложения — свой отдельный Apple Developer аккаунт** (не общий
на все). Значит у каждого приложения свои ASC API-ключ, Bundle ID и запись в App Store
Connect. **Фазы 1–3 повторяются для каждого приложения**, каждая под его собственным
Apple-аккаунтом. Подпись хранится per-job в `secrets/jobs/<job_id>/` (см.
`.claude/state/DECISIONS.md`, 2026-07-21).

## Что делает пайплайн (руками НЕ нужно)

- Перерисовывает контракт приложения под джобу: бандл, имя, Apphud/Tenjin (SwiftPM),
  AppIcon, Info.plist (encryption, ATT, SKAdNetwork).
- `xcodebuild archive` с автоподписью: сертификат и профиль создаются из ASC-ключа
  (`-allowProvisioningUpdates`), затем `exportArchive` → IPA.
- Сохраняет IPA и лог сборки (карточка **iOS build** в админке, история в `xcode_builds`).

## Предпосылки

- Активное платное членство Apple Developer Program для аккаунта приложения ($99/год).
- Джоба сгенерирована (есть SwiftUI-исходники) и находится в DONE / NEEDS_INPUT.
- Запущен Mac-воркер очереди `xcode` (Xcode, XcodeGen).

## Пошагово (повторить для каждого приложения)

### Фаза 0 — доступ
Залогинься в **appstoreconnect.apple.com** под аккаунтом приложения. Должен открыться
дашборд (не «enroll/оплата»). Иначе — сначала заверши enrollment/оплату.

### Фаза 1 — API-ключ App Store Connect
1. **Users and Access → Integrations → App Store Connect API → Team Keys**.
2. Первый раз: **Request Access** → галочка → **Submit**.
3. **＋** → Name (метка, напр. `CarPlayKey`), Access = **Admin**, **Generate**. Роль Admin
   нужна, чтобы `-allowProvisioningUpdates` сам создал distribution-сертификат и профиль
   (с App Manager Xcode этого сделать не сможет).
4. Скопируй **Issuer ID** (UUID вверху) и **Key ID** (в строке ключа); **Download API Key**
   → файл `.p8`. ⚠️ Скачивается один раз — потеряешь, придётся отзывать и создавать заново.

### Фаза 2 — Bundle ID
**developer.apple.com/account → Certificates, IDs & Profiles → Identifiers → ＋ → App IDs →
App → Continue** → Description + **Explicit Bundle ID** (напр. `com.batteam.carplay`) →
Continue → Register.

### Фаза 3 — запись приложения + Apple ID
1. **appstoreconnect.apple.com → Apps → ＋ → New App**: iOS, Name, Primary language,
   выбрать Bundle ID из фазы 2, SKU (любой уникальный, напр. бандл), Full access → **Create**.
2. Открой приложение → **App Information** → поле **Apple ID** — числовой (напр. `6752644665`).

### Фаза 4 — в админке (per-job)
1. Открой джобу приложения → вкладка **Build & sign**.
2. Карточка **App Store signing key**: загрузи `.p8` + Issuer ID + Key ID → **Save signing key**.
3. **Build settings**: профиль **Store**, Display name, Bundle ID (тот же, что в Apple),
   **App Store Apple ID** (число из фазы 3) → **Save & rebuild**. Для уже сгенерированного
   приложения это ставит нативную доставку заново: контракт перерисовывается с новым
   бандлом и ключами, модель не запускается.

### Фаза 5 — архив и IPA
Вкладка **iOS build → Archive & export IPA** (или автоматически после сборки/rework).
Mac-воркер (`-Q xcode`) делает `xcodebuild archive` + `exportArchive`
(`method=app-store-connect`, `destination=export`) и сохраняет IPA в хранилище.
Номер билда — UTC `YYMMDDHHMM.SS`, версия — `app_version` из метаданных джобы (по умолчанию
`1.0`).

Статусы карточки:
- `unsigned` — нет `XCODE_TEAM_ID` или ASC-ключа: собран неподписанный IPA (проверка сборки);
- `ready_for_upload` — подписанный IPA готов, показана команда загрузки (`altool`);
- `uploaded` — загружен (только при `IOS_DELIVERY_UPLOAD=true`);
- `upload_failed` — загрузка не прошла, IPA сохранён;
- `failed` — архив/экспорт не прошёл, причина и лог в карточке.

### Фаза 6 — загрузка в App Store Connect
По умолчанию **выключена** (`IOS_DELIVERY_UPLOAD=false`): загрузка — внешнее действие, его
включает владелец. При включении `altool --upload-app` получает ключ как
`AuthKey_<KeyID>.p8` во временном `API_PRIVATE_KEYS_DIR`. Билд появится в ASC
(обработка/TestFlight). Скриншоты, метаданные и отправка на ревью — в App Store Connect.

## Настройки

| Переменная | Назначение |
|---|---|
| `XCODE_TEAM_ID` | Team ID аккаунта Apple; без него — unsigned IPA |
| `XCODE_ARCHIVE_TIMEOUT_S` | Таймаут архива (по умолчанию 3600) |
| `IOS_DELIVERY_UPLOAD` | Разрешить загрузку в ASC (по умолчанию `false`) |
| `IOS_SIMULATOR_UDID` | Симулятор Vision Judge (пусто — первый доступный iPhone) |

CLI для существующего прогона: `uv run python -m iosforge.mvp.ios_delivery --run-dir <run> [--job-id <id>]`.
