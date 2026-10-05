# Runbook: заливка iOS-билда в App Store через Codemagic

Как довести сгенерированное приложение до билда в App Store Connect. Автоматизация
(env-var подпись + загрузка ключа в админке) заменяет бóльшую часть ручной инструкции
«Залив через Codemagic» — руками остаются только действия в аккаунтах Apple.

## Важно: у каждого приложения свой Apple-аккаунт

**У каждого клонируемого приложения — свой отдельный Apple Developer аккаунт** (не общий
на все). Значит у каждого приложения свои ASC API-ключ, Bundle ID и запись в App Store
Connect. **Фазы 1–3 повторяются для каждого приложения**, каждая под его собственным
Apple-аккаунтом. Подпись хранится per-job в `secrets/jobs/<job_id>/` (см.
`.claude/state/DECISIONS.md`, 2026-07-21).

## Что делает пайплайн (руками НЕ нужно)

- Генерирует подписанный `codemagic.yaml` (workflow `ios-store`, env-var автоподпись).
- Пушит его в репозиторий приложения, импортирует апп в Codemagic, стартует билд.
- `app-store-connect fetch-signing-files --create` создаёт distribution-сертификат +
  provisioning profile на лету из ключа; `publishing` заливает `.ipa` в App Store Connect
  (`submit_to_testflight: false` → билд ложится в ASC/обработку, без авто-ревью).
- Пропускаются шаги 7–13 PDF (сертификаты/профили) и 14–15 (правка yaml, добавление в UI).

## Предпосылки

- Активное платное членство Apple Developer Program для аккаунта приложения ($99/год).
- Джоба уже сгенерена и импортирована в Codemagic (есть `codemagic.application_id`).
  Готовые к сборке джобы: смотри админку (список джоб) или БД (`GenerationResult.codemagic`).
- `CODEMAGIC_TOKEN` лежит в `secrets/codemagic.env` (уже есть).

## Пошагово (повторить для каждого приложения)

### Фаза 0 — доступ
Залогинься в **appstoreconnect.apple.com** под аккаунтом приложения. Должен открыться
дашборд (не «enroll/оплата»). Иначе — сначала заверши enrollment/оплату.

### Фаза 1 — API-ключ App Store Connect
1. **Users and Access → Integrations → App Store Connect API → Team Keys**.
2. Первый раз: **Request Access** → галочка → **Submit**.
3. **＋** → Name (метка, напр. `CarPlayKey`), Access = **App Manager**, **Generate**.
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
   **App Store Apple ID** (число из фазы 3) → **Save & rebuild** (пересборка с реальным бандлом).

### Фаза 5 — сборка и заливка
Дождись пересборки → вкладка **iOS build → Run iOS build**. Пайплайн подпишет и зальёт `.ipa`
в App Store Connect. Билд появится в разделе билдов приложения (TestFlight/обработка).
Скриншоты, метаданные и отправка на ревью — вручную в App Store Connect.

## Первый прогон

Начинаем с **CarPlay** (`a05c37ae-fb15-43ad-9606-ba986fd33201`), под его Apple-аккаунтом.
Второе приложение — Speaker Cleaner (`ae66f4ea-4d71-44f4-bc47-14fde3cfd22b`) — отдельно,
под своим аккаунтом.
