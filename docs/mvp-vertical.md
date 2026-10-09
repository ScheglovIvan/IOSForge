# SwiftUI-вертикаль: захват → нативное приложение → Vision Judge

Сквозной путь от снятого App Store-приложения до нативного SwiftUI-клона, проверенного на
iOS Simulator. В пайплайне его запускает `swiftui_build.build_swiftui` (очередь `xcode`);
для ручных прогонов на Mac есть CLI.

## Что делает
`iosforge/mvp/`:
- `frida_ingest.py` — разворачивает Frida-архив (`docs/frida-archive.md`) в `runs/<ts>/`:
  скриншоты, `screens.json`, UIKit-дерево, шрифты, медиа, сеть, манифест захвата.
- `analyze.py` — Claude описывает экраны в `app_spec.json` (контракт v3, `spec_contract.py`).
- `swiftui_scaffold.py` / `swiftui_templates.py` — детерминированный контракт приложения:
  `ScreenID`, `AppTab`, `Router` (NavigationStack на вкладку), `AppTabBar`, headless-режим
  `-screen-id <id>`, гейт разрешений, `Subscriptions` / `Attribution`, `project.yml`.
- `swiftui_gen.py` — DAG задач без LLM-декомпозиции: тема → компоненты → экраны параллельно,
  каждый в своей песочнице; из песочницы забираются только свои пути. Compile gate:
  восстановление контракта, линт разрешений, `xcodebuild`, ограниченная петля исправлений.
  `scope_to` ужимает спеку и оригиналы судьи до одобренных экранов.
- `swiftui_media.py` — новые декоративные изображения по описанию слота и палитре клона
  (Replicate за `CODEGEN_GENERATE_IMAGES`, иначе локальные заглушки); пиксели оригинала
  не копируются.
- `simulator.py` / `xcode.py` — Simulator: закреплённое окружение (статус-бар, тема, размер
  шрифта), локаль через `-AppleLanguages` / `-AppleLocale`, стабильные кадры.
- `compliance.py` — Vision Judge: `refine_ios_until_complete` (сборка → рендер всех экранов →
  оценка → исправления экранов) поверх неизменного `_refine`; `nav_audit_ios` — статический
  аудит навигации по Swift-коду.
- `ios_delivery.py` — архив и IPA (`docs/store-upload.md`).

Поток: `archive.zip → runs/<ts>/{screens,app_spec.json} → xcode_app/ → generated_screens/ →
selftest_report.json → delivery/*.ipa`.

## Предпосылки (Mac)
- **Xcode** (с iOS Simulator runtime) и **XcodeGen** (`brew install xcodegen`).
- Хотя бы один iPhone-симулятор (`xcrun simctl list devices available`).
- **Claude Code CLI** (`claude`) авторизован локально.

## Ручной прогон
```bash
# codegen + Vision Judge по входам реальной джобы
uv run python -m iosforge.mvp.ios_pipeline \
  --app-spec path/to/app_spec.json --archive path/to/archive.zip \
  --udid <simulator-udid> --bundle-id com.example.clone --out ~/iosforge-runs
# повторная сверка существующего прогона
uv run python -m iosforge.mvp.ios_pipeline --run-dir ~/iosforge-runs/<ts> --udid <udid>
# архив + IPA существующего прогона
uv run python -m iosforge.mvp.ios_delivery --run-dir ~/iosforge-runs/<ts>
```
Опции `ios_pipeline`: `--screen` / `--exclude` (скоуп), `--locale` (иначе локаль оригинала из
спеки, манифеста захвата или витрины `--storefront`), `--max-parallel`.

Без тулчейна CLI выходят с кодом 2: молчаливого прохода вне Mac нет.

## Проверено
Speaker Cleaner: 13 экранов, 3 итерации refine, compliance 0.851 (pass), 12 оцениваемых экранов
0.85–0.95. Unsigned archive и IPA собраны (Apphud и Tenjin через SwiftPM, AppIcon в Assets.car).

## Legacy
Flutter-путь (web-рендер в Chromium, CodeMagic, GitHub-пуш исходников) удалён. Старые джобы
открываются в админке только на просмотр: артефакты, история CodeMagic-сборок, ссылка на репо.
Обход APK на Android-эмуляторе (`emulator.py`, `crawl.py`) остался как legacy-вход анализа.
