# Phase 0 spike: screen-id navigation contract (2026-10-09)

Reference implementation of `SCREEN_NAV_CONTRACT` / `SIMULATOR_ENV_CONTRACT`
(`iosforge/providers/base.py`) for the SwiftUI codegen (Phase 3). Files:
`project.yml` (XcodeGen), `Spike/SpikeApp.swift`.

Reproduce (macOS + Xcode + XcodeGen):

```sh
cd docs/swiftui-reference
xcodegen generate
xcodebuild build -project Spike.xcodeproj -scheme Spike \
  -destination 'generic/platform=iOS Simulator' -derivedDataPath build -quiet
xcrun simctl install <udid> build/Build/Products/Debug-iphonesimulator/Spike.app
xcrun simctl launch --terminate-running-process <udid> com.iosforge.spike -screen-id detail
xcrun simctl io <udid> screenshot detail.png
```

Env: Mac Apple silicon, Xcode 26.6, XcodeGen 2.46.0, iPhone 17 / iOS 26.5 (+ iPhone 15 / iOS 17.2 for URL check).
App: `Spike/SpikeApp.swift` (XcodeGen `project.yml`), 5 screens: onboarding, home, detail (NavigationStack + fixture), settings, paywall (sheet).

## Results
| Check | Result |
|---|---|
| `xcodebuild build -destination 'generic/platform=iOS Simulator'` | OK, 7 s (clean), CODE_SIGNING_ALLOWED=NO |
| Simulator cold boot | 32 s |
| `simctl launch --terminate-running-process <udid> <bundle> -screen-id <id>` | OK for all 5 screens incl. pushed detail and modal sheet |
| Unknown id | explicit error screen rendered |
| Headless mode suppresses onboarding + notification prompt | OK (normal launch shows the system prompt and blocks the screen) |
| `simctl openurl iosforge://screen/<id>` | **FAILS headless**: system dialog "Open in «Spike»?" on cold AND foreground, iOS 26.5 AND 17.2 |
| Launch → stable frame | ~0.7 s (0.2 s: 5–70% px diff, 0.4 s: 1–11%) |
| Screenshot determinism | md5 unusable (status bar / home indicator AA); pixel diff ≤0.2% |

## Proposed contract changes (SCREEN_NAV_CONTRACT, providers/base.py) — via architect
1. Launch argument `-screen-id <id>` is the ONLY headless entry point. URL scheme → optional (manual QA), never used by the judge.
2. In screen-id mode the app MUST: skip onboarding/first-run gates; suppress ALL system permission prompts (notifications, ATT, location, …); render data-dependent screens from fixtures; present modal screens (sheet/fullScreenCover) as the target.
3. Unknown id → explicit error screen (detectable by judge / nav_audit).
4. Worker readiness = poll screenshots until 2 consecutive frames differ <0.5%, not fixed sleep.
5. Worker pins environment: `simctl status_bar override`, locale/language (sim default here was ru), appearance, `--terminate-running-process` per screen.
6. Image comparison: pixel/perceptual diff with tolerance, never byte hashes.
