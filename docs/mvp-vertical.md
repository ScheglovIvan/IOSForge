# MVP-вертикаль (фаза 1)

Сквозная вертикаль «локальный APK → обход на эмуляторе → скриншоты+карта экранов →
локальный Claude Code → Flutter-приложение». Намеренно БЕЗ провайдер-абстракций, очереди,
машины состояний, админки — это всё фаза 2 (`.claude/state/BACKLOG.md`). Один линейный проход,
хардкод где можно.

## Что делает
`iosforge/mvp/`:
- `cli.py` — точка входа: `run <app.apk>`; оркеструет шаги, создаёт `runs/<ts>/`.
- `emulator.py` — adb-обёртка: старт AVD (headless), install APK, launch.
- `crawl.py` — мини-краулер на голом adb: `screencap` + `uiautomator dump` → `screens/NNNN.png` + `screens.json`.
- `claude_gen.py` — готовит `claude_ws/` (скриншоты + карта + `PROMPT.md`), зовёт `claude -p`, проверяет/переносит `flutter_app/`.
- `paths.py` — единая раскладка `runs/<ts>/`.

Поток: `apk → emulator → screens/*.png + screens.json → claude_ws/ → flutter_app/`.

## Предпосылки окружения (ставятся вне репозитория)
- **Android SDK** в `/opt/android-sdk` (`platform-tools`=adb, `emulator`, `system-images;android-34;google_apis;x86_64`), AVD с именем `mvp`. Требуется `/dev/kvm` (есть на хосте).
- **X11/GL-библиотеки** для headless-эмулятора: `libx11-xcb1 libgl1 …` (иначе эмулятор падает на `libX11-xcb.so.1`).
- **Flutter SDK** в `/opt/flutter` (для `flutter analyze` build-check). Под root flutter лишь предупреждает, анализ проходит.
- **Claude Code CLI** (`/usr/bin/claude`) — авторизован локально (НЕ API-токен), §5.5.
- JDK (для Android cmdline-tools), `unzip`.

## Запуск
```bash
export ANDROID_SDK_ROOT=/opt/android-sdk
# эмулятор (если не запущен):
$ANDROID_SDK_ROOT/emulator/emulator -avd mvp -no-window -no-audio -no-boot-anim \
  -gpu swiftshader_indirect -no-snapshot -no-metrics &
# вертикаль:
uv run python -m iosforge.mvp.cli run path/to/app.apk --max-screens 8
```
Опции: `--out runs` (база вывода), `--max-screens N`, `--avd mvp`, `--no-build-check`.
Выход: `runs/<ts>/flutter_app/` (исходники Flutter) + `runs/<ts>/screens.json` (карта).

## Проверено
Прогон на OpenSudoku (`org.moire.opensudoku`): install → обход 3 экранов → Claude сгенерил
`flutter_app/` (`pubspec.yaml`, `lib/main.dart`, по виджету на экран) → `flutter analyze` =
«No issues found!».

## Известные ограничения (в фазу 2)
- Краулер мелкий: тапает первый неиспробованный clickable, дедуп по сигнатуре UI; на тяжёлых
  приложениях (ANR на старте, как F-Droid) обход вырождается. Глубину/стратегию — в фазе 2 (§5.4).
- `activity` в карте часто `unknown` (regex `mResumedActivity` не всегда матчит) — некритично для генерации.
- Генерация — один проход, без итеративной сверки до ≥95% (§5.5 — фаза 2).
- iOS-сборка не делается (нужен macOS/Codemagic — фаза 2, см. `DECISIONS.md`).
