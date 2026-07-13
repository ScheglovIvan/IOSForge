"""Hand the crawl output to the local Claude Code CLI and collect a Flutter app.

Prepares claude_ws/ (screenshots + screens.json + PROMPT.md), invokes `claude -p`
non-interactively in that dir, then verifies/moves the generated flutter_app/.
One pass, no 95% loop (phase 2).
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import structlog

from iosforge.common.logging import get_logger
from iosforge.mvp.analyze import stage_archive_context, topo_layers
from iosforge.mvp.paths import RunPaths

log = get_logger("mvp.claude_gen")

CLAUDE_BIN = "claude"

_WORKTREE_LOCK = threading.Lock()

PROMPT = """\
You are generating a Flutter app that visually reproduces an existing Android app.

Inputs in this directory:
- `screens.json` — crawled screens: each has a screenshot path, the current activity,
  the clickable `elements` (text / resource_id / bounds), and `from`/`tapped` links
  showing how screens connect.
- `screens/` — one PNG screenshot per screen.

Task (static UI + navigation only, NO backend/business logic):
1. Look at every screenshot and the screens.json structure.
2. Create a Flutter app under `flutter_app/` with a standard layout:
   - `flutter_app/pubspec.yaml`
   - `flutter_app/lib/main.dart` (app entry + routing)
   - one widget file per distinct screen under `flutter_app/lib/screens/`.
3. Reproduce each screen's layout (app bars, lists, buttons, text) as faithfully as
   you can from the screenshots, and wire navigation between screens following the
   `from`/`tapped` links.
4. Use only the Flutter SDK + material widgets. No network calls, no external packages
   beyond what ships with Flutter. Keep it compiling.

Output ONLY the files under `flutter_app/`. Do not run the app.
"""


def _prepare_workspace(paths: RunPaths) -> None:
    paths.claude_ws.mkdir(parents=True, exist_ok=True)
    ws_screens = paths.claude_ws / "screens"
    if ws_screens.exists():
        shutil.rmtree(ws_screens)
    shutil.copytree(paths.screens_dir, ws_screens)
    shutil.copy2(paths.screens_json, paths.claude_ws / "screens.json")
    (paths.claude_ws / "PROMPT.md").write_text(PROMPT)


def generate(paths: RunPaths, timeout: int = 1800) -> Path:
    """Run Claude Code over the crawl output; return the path to the Flutter app."""
    _prepare_workspace(paths)
    log.info("claude_gen.invoking", workdir=str(paths.claude_ws))
    res = subprocess.run(
        [CLAUDE_BIN, "-p", PROMPT, "--permission-mode", "acceptEdits"],
        cwd=paths.claude_ws,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if res.returncode != 0:
        log.error("claude_gen.cli_failed", code=res.returncode, stderr=res.stderr[-2000:])
        raise RuntimeError(f"claude CLI exited {res.returncode}: {res.stderr[-500:]}")

    generated = paths.claude_ws / "flutter_app"
    pubspec = generated / "pubspec.yaml"
    main_dart = generated / "lib" / "main.dart"
    if not (pubspec.exists() and main_dart.exists()):
        raise RuntimeError(
            "Claude did not produce a valid flutter_app/ "
            f"(pubspec={pubspec.exists()}, main.dart={main_dart.exists()})"
        )
    if paths.flutter_app.exists():
        shutil.rmtree(paths.flutter_app)
    shutil.move(str(generated), str(paths.flutter_app))
    log.info("claude_gen.done", flutter_app=str(paths.flutter_app))
    return paths.flutter_app


def _prepare_task_workspace(paths: RunPaths) -> None:
    """Stage crawl + analysis artifacts into the persistent task-runner workspace.

    Reuses :func:`_prepare_workspace` (screens/, screens.json) and adds
    app_spec.json + tasks.json. The single-pass PROMPT.md is removed so only the
    per-task TASK.md instruction is present and cannot confuse a task stage.
    flutter_app/ is intentionally left untouched so it accumulates across
    per-task invocations.
    """
    _prepare_workspace(paths)
    (paths.claude_ws / "PROMPT.md").unlink(missing_ok=True)
    shutil.copy2(paths.app_spec_json, paths.claude_ws / "app_spec.json")
    shutil.copy2(paths.tasks_json, paths.claude_ws / "tasks.json")
    if paths.rc_config_json.exists():
        shutil.copy2(paths.rc_config_json, paths.claude_ws / "rc_config.json")
    summary = stage_archive_context(paths, paths.claude_ws, include_bytes=True)
    log.info("codegen_tasks.archive_context", **summary)


def _prepare_rework_workspace(paths: RunPaths) -> None:
    """Stage the EXISTING flutter_app + spec + screens + archive context for rework.

    The already-generated app (hydrated into ``paths.flutter_app``) is copied into
    the workspace so the rework runs edit it in place with full ground truth on hand.
    """
    _prepare_workspace(paths)
    (paths.claude_ws / "PROMPT.md").unlink(missing_ok=True)
    if paths.app_spec_json.exists():
        shutil.copy2(paths.app_spec_json, paths.claude_ws / "app_spec.json")
    if paths.rc_config_json.exists():
        shutil.copy2(paths.rc_config_json, paths.claude_ws / "rc_config.json")
    stage_archive_context(paths, paths.claude_ws, include_bytes=True)
    ws_app = paths.claude_ws / "flutter_app"
    if ws_app.exists():
        shutil.rmtree(ws_app)
    shutil.copytree(paths.flutter_app, ws_app)


def _rework_prompt(instructions: str) -> str:
    """Inline prompt for a user-driven rework pass over the existing app."""
    return f"""\
You are REWORKING an existing Flutter app under `flutter_app/` in this directory.
The app was already generated and works; now fix the reported issues WITHOUT a rewrite.

Context files (read as needed):
- `flutter_app/` — the CURRENT app. EDIT IT IN PLACE. Preserve every screen and
  feature that already works; change only what the report below requires.
- `app_spec.json` — the app's structured spec (screens, content, monetization, audio).
- `screens.json` + `screens/` — the original target screenshots (ground truth for look).
- `source/<id>.json`, `fonts/`, `media/` (if present) — real geometry, fonts and media
  (images/video/audio) to reuse when a fix needs an asset.

Reported issues / requested changes:
{instructions}

Rules:
- Make the smallest change that fixes each reported item; do NOT restructure or
  regenerate working code, and do NOT delete unrelated files.
- Keep the app compiling (Flutter SDK + the packages already in pubspec; add a
  package only if a fix truly needs it). Keep the `iosforge://screen/<id>` deep links
  and the `/#/screen/<id>` web preview routes working.
- Reuse bundled assets (`fonts/`, `media/`, existing `assets/`) rather than inventing new ones.
- NO ADS: never add advertising (ad SDKs, banners, interstitials, rewarded ads) — this
  clone ships ad-free. If you spot a leftover ad slot or an empty gap where an ad used to
  be, remove it and let the layout collapse/reflow so no gap remains. Paywalls/subscriptions
  are fine.

Output ONLY changes under `flutter_app/`. Do not run the app.
"""


_AUGMENT_PROMPT = """\
You are INCREMENTALLY completing an existing Flutter app under `flutter_app/` — NOT
regenerating it. It was generated in a previous pass and works; ADD only what is
MISSING, reusing and preserving all existing working code.

Context files (read as needed):
- `flutter_app/` — the CURRENT app. EDIT IN PLACE; do NOT rewrite or delete working
  files. Read what already exists before adding anything.
- `app_spec.json` — the target spec (screens, navigation, monetization, content, audio).
- `rc_config.json` (OPTIONAL) — RevenueCat config (`sdk_key`, `entitlement`, `offering`,
  `mode`); present only if the app should monetize via RevenueCat.
- `screens.json` + `screens/`, `source/<id>.json`, `fonts/`, `media/` — ground truth.

Do a GAP ANALYSIS and add ONLY the missing pieces:
1. SCREENS: any `app_spec.json` screen id with no widget / no route in the app — add it
   (compose from `lib/ui/components/` if that library exists), wired into the router and
   the `/screen/:id` preview + `iosforge://screen/<id>` deep link.
2. NAVIGATION: any `navigation.map` edge (from→to) not reachable — wire it.
3. REVENUECAT: if `rc_config.json` exists and the app does not already configure
   RevenueCat, add `purchases_flutter` and `Purchases.configure(PurchasesConfiguration(
   "<sdk_key>"))` in `main()` (try/catch, guard empty key), and make the paywall use
   `Purchases.getOfferings()` (`offerings.current`), `purchasePackage`, `restorePurchases`
   and unlock premium when `customerInfo.entitlements.active` is non-empty.
4. Anything else in `app_spec.json` clearly not yet implemented (audio triggers, etc.).

Rules: smallest additive change; keep it compiling; do NOT change the divergent design
or restructure working screens; do NOT add ads. Output ONLY changes under `flutter_app/`.
"""


def augment(paths: RunPaths, *, timeout: int = 1800) -> Path:
    """Incremental pass: ADD what's missing to the existing flutter_app/ (no rebuild)."""
    bound = log.bind(stage="augment", run_dir=str(paths.run_dir))
    if not paths.flutter_app.exists():
        raise RuntimeError(f"no existing flutter_app to augment: {paths.flutter_app}")
    _prepare_rework_workspace(paths)
    bound.info("augment.invoking", workdir=str(paths.claude_ws))
    run_task(paths.claude_ws, _AUGMENT_PROMPT, timeout=timeout, tlog=bound)

    ws_app = paths.claude_ws / "flutter_app"
    pubspec = ws_app / "pubspec.yaml"
    main_dart = ws_app / "lib" / "main.dart"
    if not (pubspec.exists() and main_dart.exists()):
        raise RuntimeError("augment left an invalid flutter_app/ (pubspec/main.dart missing)")
    if paths.flutter_app.exists():
        shutil.rmtree(paths.flutter_app)
    shutil.move(str(ws_app), str(paths.flutter_app))
    bound.info("augment.done", flutter_app=str(paths.flutter_app))
    return paths.flutter_app


def rework(paths: RunPaths, instructions: str, *, timeout: int = 1800) -> Path:
    """Apply a user-driven rework to the existing flutter_app/; return its path."""
    bound = log.bind(stage="rework", run_dir=str(paths.run_dir))
    if not paths.flutter_app.exists():
        raise RuntimeError(f"no existing flutter_app to rework: {paths.flutter_app}")
    _prepare_rework_workspace(paths)
    bound.info("rework.invoking", workdir=str(paths.claude_ws))
    run_task(paths.claude_ws, _rework_prompt(instructions), timeout=timeout, tlog=bound)

    ws_app = paths.claude_ws / "flutter_app"
    pubspec = ws_app / "pubspec.yaml"
    main_dart = ws_app / "lib" / "main.dart"
    if not (pubspec.exists() and main_dart.exists()):
        raise RuntimeError("rework left an invalid flutter_app/ (pubspec/main.dart missing)")
    if paths.flutter_app.exists():
        shutil.rmtree(paths.flutter_app)
    shutil.move(str(ws_app), str(paths.flutter_app))
    bound.info("rework.done", flutter_app=str(paths.flutter_app))
    return paths.flutter_app


_NO_ADS_LINE = (
    "\nNO ADS: this clone ships ad-free. Do NOT add ad SDKs (google_mobile_ads / "
    "AppLovin / etc.) or ad widgets. If the original screen (screenshot or spec) has "
    "an ad banner / interstitial / native slot, do NOT recreate it AND do NOT leave "
    "an empty gap or reserved banner strip — remove it entirely and let the surrounding "
    "layout collapse/expand (e.g. drop the SizedBox/Container, let the column reflow) so "
    "the screen looks natural, as if it never had an ad. Keep paywalls/subscriptions.\n"
)


_CONTENT_DIVERGENCE_NOTE = """
ANTI-CLONE CONTENT: this clone must not look or read like the source app.
- COPY: paraphrase every user-facing string (titles, labels, buttons, onboarding,
  empty/error states, marketing) to the SAME meaning with clearly DIFFERENT wording;
  never reproduce the original's copy verbatim. Keep functional/legal terms accurate.
- ICONS: use a single, consistent icon style that DIFFERS from the original (e.g. a
  different Material icon variant/set); do NOT reproduce the source app's exact icons.
- BRANDING: do NOT bundle or render the source app's logo / wordmark / brand glyphs;
  use the design system's own icon for the app mark. Keep photographic content assets
  (hero / paywall / thumbnails) — those convey content, not brand identity.
"""


_RC_NOTE = """
REVENUECAT (ONLY if `rc_config.json` exists in this directory): the app monetizes via
RevenueCat. Read `rc_config.json` (fields: `sdk_key`, `entitlement`, `offering`, `mode`).
- SCAFFOLD: add `purchases_flutter: ^8.0.0` to pubspec (use THIS constraint — older
  6.x/7.x fail to compile on current Xcode with `'SubscriptionPeriod' is ambiguous`);
  in `main()` before `runApp`, call
  `await Purchases.configure(PurchasesConfiguration("<sdk_key>"))` inside a try/catch and
  guard an empty key so it never crashes. When `mode` is `test_store` the key drives
  RevenueCat's virtual Test Store, so purchases work without an App Store build.
- PAYWALL screen: load offerings via `Purchases.getOfferings()`, render the packages of
  `offerings.current` (fall back to the offering whose identifier is `<offering>` if
  present) — prices come FROM the offering, never hardcode them; buy with
  `Purchases.purchasePackage(pkg)`, restore with `Purchases.restorePurchases()`, and
  unlock premium when `customerInfo.entitlements.active` is non-empty (or contains
  `<entitlement>`).
Testable via the RevenueCat Test Store / StoreKit sandbox (no live App Store link yet).
"""


def _task_prompt(task: dict[str, object], no_ads: bool = True, diverge_content: bool = True) -> str:
    """Inline per-task prompt for the task runner.

    Inlined to match the existing single-pass prompt; moving prompts to a managed
    PromptSetProvider (SPEC §6) is deferred tech-debt for the MVP vertical.
    """
    screens = task.get("screens", [])
    screen_ids = [str(s) for s in screens] if isinstance(screens, list) else []
    shots = "\n".join(f"- screens/{sid}.png" for sid in screen_ids) or "- (none)"
    sources = "\n".join(f"- source/{sid}.json" for sid in screen_ids) or "- (none)"
    no_ads_line = _NO_ADS_LINE if no_ads else ""
    content_divergence_note = _CONTENT_DIVERGENCE_NOTE if diverge_content else ""
    content_line = (
        "Preserve each screen's FUNCTION and content MEANING, but diverge its copy and "
        "icons per the ANTI-CLONE CONTENT rules below (paraphrase text, restyle icons, "
        "drop source branding); keep photographic media."
        if diverge_content
        else "Keep the original icons, images, text/copy and media."
    )
    return f"""\
You are incrementally building a Flutter app under `flutter_app/` in this directory.

Context files (read as needed):
- `app_spec.json` — the full structured spec for the target app.
- `screens.json` — the raw crawl with element bounds and navigation links.
- `source/<id>.json` (OPTIONAL) — the EXACT native view hierarchy per screen:
  recursive nodes with `frame` (x/y/w/h in points, top-left), `text`, `font`
  (postscript_name/family/point_size/weight/italic), `colors` (#RRGGBBAA),
  `layer` (corner_radius/border/opacity/shadow), `kind` (image|video|animation)
  and `asset_ref.media_id`. GROUND TRUTH for layout, typography and color.
- `fonts.json` (OPTIONAL) — real fonts; bundled ones are files under `fonts/`.
- `media.json` (OPTIONAL) — real media assets (`id`, `role`, `kind`, `path`);
  byte files live under `media/`.
- `flutter_app/` — the app so far. EDIT IT IN PLACE. Do not delete or rewrite
  files that other tasks created unless this task requires it.

Current task:
- id: {task.get("id")}
- type: {task.get("type")}
- title: {task.get("title")}

Relevant screenshots to LOOK at (vision):
{shots}
Matching native layout ground truth (read if present):
{sources}

Do exactly the work this task describes and nothing more:
- If type is "scaffold": create the Flutter project skeleton —
  `flutter_app/pubspec.yaml`, `flutter_app/lib/main.dart` (app entry + theme +
  routing). Keep it compiling.
  FONTS: use the DIVERGENT families named in `app_spec.json`
  `design_tokens.font.*` (these are similar-but-different substitutes, not the
  original app's fonts). Add the `google_fonts` package to pubspec and apply the
  primary family via `GoogleFonts` in the app `ThemeData` text theme so every
  screen inherits it. Do NOT bundle the original `fonts/` `.ttf` files and do NOT
  reuse the source app's font families.
  THEME: derive the app's design language from `app_spec.json` `design_tokens` —
  `ThemeData` colors from `design_tokens.color`, corner radii from
  `design_tokens.dimension`, and expose the `design_tokens.gradient.*` entries
  (angle + stops) as reusable `LinearGradient`s in a `lib/ui/theme/` helper.
  Honor `design_tokens.button_style` (filled|tonal|outlined) and `elevation_style`
  for the default button/card look. These tokens are a DELIBERATELY DIVERGENT
  design (new palette/fonts/gradients) — build exactly what the tokens say; do NOT
  fall back to colors/fonts you see in the screenshots.
  You MAY add `video_player` (for .mp4 backgrounds), `lottie` (for Lottie `.json`)
  and `audioplayers` (for `kind:"audio"` sounds) to pubspec ONLY if archive media
  needs them; otherwise stay on the Flutter SDK + material widgets.
  The app MUST support deep-link navigation `iosforge://screen/<id>` that routes
  directly to the screen whose id matches `<id>` (the same ids used in
  `app_spec.json` / `screens.json`). Add an `<intent-filter>` with
  `<data android:scheme="iosforge"/>` to `android/app/src/main/AndroidManifest.xml`
  and a router (e.g. `onGenerateRoute` / a platform deep-link handler) that parses
  the incoming URI host/path and shows the matching screen.
  ALSO register a canonical web preview route `/screen/:id` (reachable in a browser
  at `/#/screen/<id>`) that shows the same screen for that `<id>` — this is used to
  verify each screen in headless Chromium, so it MUST render standalone.
  PARALLEL-SAFE WIRING (important): screens are built independently and in parallel,
  so the router MUST be COMPLETE now. For EVERY screen id in `app_spec.json`, create a
  placeholder file `lib/features/<id>/<id>_screen.dart` exporting a widget class
  `Screen_<id>` (a simple stub for now) and register `/<id>`, `/screen/:id` and the
  `iosforge://screen/<id>` deep link to that class by importing that exact file. A
  later screen task will OVERWRITE only `lib/features/<id>/` with the real UI (same
  file path + class name), so it never needs to edit `lib/main.dart`,
  `lib/core/router/` or `pubspec.yaml`. Pre-declare `assets/`, `assets/media/` and
  `assets/fonts/` in pubspec so screen tasks can drop files without editing pubspec.
  NAVIGATION PRESENTATION (allowed to diverge ~20%): you MAY restyle how navigation
  is presented — regroup/reorder bottom-tab or drawer items, restyle the nav bar via
  the component library, change page transitions/animations, and add wrapper/shell
  screens. You MUST NOT change reachability: every screen id in `app_spec.json` stays
  reachable, every `navigation.map` edge (`from`->`to`) and every `deep_links` entry
  MUST still resolve, and the `/<id>`, `/screen/:id` and `iosforge://screen/<id>`
  routes MUST NOT be renamed or removed (a structural nav audit fails the build if any
  edge or route is dropped).
- If type is "component_library": create the shared, reusable UI widgets that ALL
  screens compose from — the app's "design system". Put them under
  `flutter_app/lib/ui/components/` (e.g. `AppButton`, `AppCard`, `AppTextField`,
  `AppTopBar`, `AppBottomBar`, `AppListTile`, `AppChip`, `AppBadge`,
  `AppSectionHeader`, `AppScaffold`). Each widget MUST consume ONLY the divergent
  `design_tokens` (colors, `gradient.*`, radii, `button_style`, fonts via the theme)
  — no hardcoded colors/fonts. Write a manifest `lib/ui/components/manifest.json`
  as a JSON array `[{{"name","file","props":[...],"when_to_use"}}]` and a short
  `lib/ui/components/COMPONENTS.md`. Cover every recurring block seen across the
  screens so screen tasks never need to invent new styled widgets. Keep it
  compiling; do NOT build any screen here.
- Otherwise (screen/flow/state/polish): ADD or EDIT files under
  `flutter_app/lib/features/<id>/` (one directory per screen id) to implement this
  task, reusing the existing scaffold, theme and router. COMPOSE the UI ONLY from
  `lib/ui/components/` (read `lib/ui/components/manifest.json`) — do NOT invent new
  colors, fonts, gradients or button/card styling; those are FROZEN in the component
  library and theme. When a `source/<id>.json` exists, use it ONLY for LAYOUT —
  block placement/order, sizing and hierarchy from `frame`, and which component fits
  each block — NOT for color, font family or corner rounding (the design system owns
  the look). {content_line}
  MEDIA: for an image/video/animation node with `asset_ref.media_id`, look that
  id up in `media.json`, copy its `path` file from `media/` into
  `flutter_app/assets/media/`, declare it in pubspec `assets:`, and render it
  (`Image.asset` for images, `video_player` for `.mp4`, `lottie` for Lottie
  `.json`). For a full-bleed background of a splash or paywall screen whose node
  is not joined (a SwiftUI overlay, `asset_ref.resolved:false` or no node), pick
  the `media.json` entry by `role` (`splash_background` / `paywall_hero`) and use
  it as that screen's background layer.
  AUDIO: sound is often the app's core feature. For an "audio" task (and any screen
  that triggers sound), read `app_spec.json` `content.audio` and `media.json`
  (`kind:"audio"`): copy each sound file from `media/` into `flutter_app/assets/audio/`,
  declare `assets/audio/` in pubspec `assets:`, add `audioplayers` to pubspec, and
  play each sound on its mapped `trigger` (e.g. `AudioPlayer().play(AssetSource(...))`
  on the tap/screen the trigger names). Wire EVERY `content.audio` entry — the clone
  must play the same sounds on the same actions as the original.
{no_ads_line}{content_divergence_note}{_RC_NOTE}
Output ONLY changes under `flutter_app/`. Do not run the app.
"""


def run_task(
    workspace: Path,
    prompt: str,
    *,
    timeout: int,
    tlog: structlog.stdlib.BoundLogger,
) -> int:
    """Write TASK.md and run one `claude -p` task invocation in ``workspace``.

    Shared by the Stage D task runner (serial and per-worktree parallel) and the
    Stage E corrective runner. Returns the CLI return code; a non-zero code is
    logged as a warning (best-effort, the caller decides whether the resulting
    flutter_app/ is still valid).
    """
    (workspace / "TASK.md").write_text(prompt)
    res = subprocess.run(
        [CLAUDE_BIN, "-p", prompt, "--permission-mode", "acceptEdits"],
        cwd=workspace,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if res.returncode != 0:
        tlog.warning("codegen_tasks.task.cli_failed", code=res.returncode, stderr=res.stderr[-500:])
    return res.returncode


def _slug(value: str) -> str:
    """Collision-free git-safe name: readable prefix + a hash of the raw value."""
    safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in value) or "task"
    return f"{safe}-{hashlib.sha1(value.encode()).hexdigest()[:8]}"


def _owned_paths(task: dict[str, object]) -> list[str]:
    """Repo-relative paths a screen task is allowed to write (merge whitelist).

    Uses the RAW screen ids — they match the `lib/features/<id>/` directories the
    scaffold pre-registers and the screen task writes.
    """
    screens = task.get("screens")
    ids = [str(s) for s in screens] if isinstance(screens, list) and screens else [str(task["id"])]
    owned = [f"lib/features/{sid}" for sid in ids]
    owned.append("assets")
    return owned


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-c", "user.email=codegen@iosforge.local", "-c", "user.name=iosforge", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )


def _git_base_commit(flutter_app: Path) -> None:
    """Snapshot the current app so screen worktrees branch off the latest state.

    Initialises the repo on first use, then always commits any pending edits
    (e.g. serial tasks that ran between screen layers) so ``worktree add HEAD``
    branches from an up-to-date base.
    """
    if not (flutter_app / ".git").exists():
        _git(["init", "-q"], flutter_app)
    _git(["add", "-A"], flutter_app)
    _git(["commit", "-q", "-m", "codegen base snapshot", "--allow-empty"], flutter_app)


def _run_task_attempts(
    workspace: Path,
    task: dict[str, object],
    *,
    index: int,
    total: int,
    require_pubspec: bool,
    no_ads: bool,
    diverge_content: bool,
    timeout: int,
    max_attempts: int,
    on_task: Callable[[int, int, str, str, str, int], None] | None,
    tlog: structlog.stdlib.BoundLogger,
) -> bool:
    """Run one task in ``workspace`` with retries; return True on success."""
    tid = str(task.get("id"))
    title = str(task.get("title") or tid)
    pubspec = workspace / "flutter_app" / "pubspec.yaml"
    attempts = 0
    while attempts < max(1, max_attempts):
        attempts += 1
        status = "running" if attempts == 1 else "retrying"
        if on_task is not None:
            on_task(index, total, tid, title, status, attempts)
        tlog.info("codegen_tasks.task.start", index=index, attempt=attempts)
        prompt = _task_prompt(task, no_ads, diverge_content)
        code = run_task(workspace, prompt, timeout=timeout, tlog=tlog)
        if code == 0 and (not require_pubspec or pubspec.exists()):
            tlog.info("codegen_tasks.task.done", index=index, attempts=attempts)
            if on_task is not None:
                on_task(index, total, tid, title, "done", attempts)
            return True
        tlog.warning("codegen_tasks.task.attempt_failed", index=index, attempt=attempts)
    if on_task is not None:
        on_task(index, total, tid, title, "failed", attempts)
    return False


def _build_screen_in_worktree(
    paths: RunPaths,
    task: dict[str, object],
    flutter_app: Path,
    *,
    index: int,
    total: int,
    no_ads: bool,
    diverge_content: bool,
    timeout: int,
    max_attempts: int,
    on_task: Callable[[int, int, str, str, str, int], None] | None,
    tlog: structlog.stdlib.BoundLogger,
) -> tuple[str, bool]:
    """Build one screen task in an isolated git worktree; return (branch, ok)."""
    tid = str(task["id"])
    branch = f"task/{_slug(tid)}"
    wt_root = paths.claude_ws / "wt" / _slug(tid)
    if wt_root.exists():
        shutil.rmtree(wt_root)
    wt_root.mkdir(parents=True)
    for item in paths.claude_ws.iterdir():
        if item.name in ("flutter_app", "wt", "TASK.md"):
            continue
        os.symlink(item, wt_root / item.name)
    wt_app = wt_root / "flutter_app"
    with _WORKTREE_LOCK:
        added = _git(["worktree", "add", "-q", "-b", branch, str(wt_app), "HEAD"], flutter_app)
    if added.returncode != 0:
        tlog.warning("codegen_tasks.worktree.add_failed", branch=branch, stderr=added.stderr[-300:])
        return branch, False
    ok = _run_task_attempts(
        wt_root,
        task,
        index=index,
        total=total,
        require_pubspec=False,
        no_ads=no_ads,
        diverge_content=diverge_content,
        timeout=timeout,
        max_attempts=max_attempts,
        on_task=on_task,
        tlog=tlog,
    )
    if ok:
        _git(["add", "-A"], wt_app)
        _git(["commit", "-q", "-m", f"build {branch}", "--allow-empty"], wt_app)
    return branch, ok


def _merge_screen(flutter_app: Path, branch: str, owned: list[str]) -> None:
    """Pull only the owned paths from a screen's branch into the canonical app."""
    for path in owned:
        res = _git(["checkout", branch, "--", path], flutter_app)
        if res.returncode != 0:
            log.info("codegen_tasks.merge.skip_path", branch=branch, path=path)
    _git(["add", "-A"], flutter_app)
    _git(["commit", "-q", "-m", f"merge {branch}", "--allow-empty"], flutter_app)


def _cleanup_worktrees(paths: RunPaths, flutter_app: Path) -> None:
    wt_dir = paths.claude_ws / "wt"
    if wt_dir.exists():
        for child in wt_dir.iterdir():
            _git(["worktree", "remove", "--force", str(child / "flutter_app")], flutter_app)
        shutil.rmtree(wt_dir, ignore_errors=True)
    _git(["worktree", "prune"], flutter_app)


def generate_from_tasks(
    paths: RunPaths,
    timeout: int = 1800,
    *,
    completed: set[str] | None = None,
    on_task_done: Callable[[str], None] | None = None,
    on_plan: Callable[[int], None] | None = None,
    on_task: Callable[[int, int, str, str, str, int], None] | None = None,
    max_attempts: int = 1,
    strict: bool = False,
    no_ads: bool = True,
    diverge_content: bool = True,
    max_parallel: int = 1,
) -> Path:
    """Stage D: build flutter_app/ from tasks.json in dependency LAYERS; return its path.

    Serial foundation (scaffold + component_library + flow/state/…) accumulates in
    the canonical workspace flutter_app/. When ``max_parallel > 1`` a layer with
    several independent ``screen`` tasks is built concurrently: each screen runs in
    its own git worktree branched off the frozen foundation, then only its owned
    paths (``lib/features/<id>/`` + ``assets``) are merged back — disjoint ownership
    keeps merges conflict-free (the scaffold pre-registers every route to a
    per-feature stub so screen workers never touch the shared router/main/pubspec).

    Resumable: tasks whose id is in ``completed`` are skipped; ``on_task_done`` fires
    after each freshly-finished serial task and after each screen is merged. Progress
    ``on_plan``/``on_task`` and per-task ``max_attempts`` retry/``strict`` semantics
    are unchanged.
    """
    bound = log.bind(stage="codegen_tasks", run_dir=str(paths.run_dir))
    if not paths.app_spec_json.exists():
        raise RuntimeError(f"app_spec.json missing; run analyze first: {paths.app_spec_json}")
    if not paths.tasks_json.exists():
        raise RuntimeError(f"tasks.json missing; run decompose first: {paths.tasks_json}")

    _prepare_task_workspace(paths)
    payload = json.loads(paths.tasks_json.read_text())
    tasks = payload.get("tasks") if isinstance(payload, dict) else None
    if not isinstance(tasks, list) or not tasks:
        raise RuntimeError("tasks.json has an empty or missing 'tasks' array")
    layers = topo_layers(tasks)
    ordered = [t for layer in layers for t in layer]
    total = len(ordered)
    index_of = {str(t["id"]): i for i, t in enumerate(ordered)}
    if on_plan is not None:
        on_plan(total)
    done: set[str] = set(completed or set())
    flutter_app = paths.claude_ws / "flutter_app"
    pubspec = flutter_app / "pubspec.yaml"
    main_dart = flutter_app / "lib" / "main.dart"

    def _report_skip(task: dict[str, object]) -> None:
        tid = str(task["id"])
        if on_task is not None:
            on_task(index_of[tid], total, tid, str(task.get("title") or tid), "done", 0)

    def _run_serial(task: dict[str, object]) -> None:
        tid = str(task["id"])
        index = index_of[tid]
        tlog = bound.bind(task_id=tid, task_type=str(task.get("type")))
        ok = _run_task_attempts(
            paths.claude_ws,
            task,
            index=index,
            total=total,
            require_pubspec=index == 0,
            no_ads=no_ads,
            diverge_content=diverge_content,
            timeout=timeout,
            max_attempts=max_attempts,
            on_task=on_task,
            tlog=tlog,
        )
        if not ok:
            if index == 0 or strict:
                raise RuntimeError(f"codegen task {tid!r} failed permanently")
            tlog.warning("codegen_tasks.task.failed_lenient", index=index)
            return
        done.add(tid)
        if on_task_done is not None:
            on_task_done(tid)

    for layer in layers:
        pending = [t for t in layer if str(t["id"]) not in done]
        for task in (t for t in layer if str(t["id"]) in done):
            _report_skip(task)
        screen_tasks = [t for t in pending if str(t.get("type")) == "screen"]
        serial_tasks = [t for t in pending if str(t.get("type")) != "screen"]
        for task in serial_tasks:
            _run_serial(task)
        if max_parallel > 1 and len(screen_tasks) > 1:
            _run_screen_layer(
                paths,
                screen_tasks,
                flutter_app,
                total=total,
                index_of=index_of,
                done=done,
                no_ads=no_ads,
                diverge_content=diverge_content,
                timeout=timeout,
                max_attempts=max_attempts,
                strict=strict,
                max_parallel=max_parallel,
                on_task=on_task,
                on_task_done=on_task_done,
                bound=bound,
            )
        else:
            for task in screen_tasks:
                _run_serial(task)

    if (flutter_app / ".git").exists():
        shutil.rmtree(flutter_app / ".git", ignore_errors=True)
    if not (pubspec.exists() and main_dart.exists()):
        raise RuntimeError(
            "task runner did not produce a valid flutter_app/ "
            f"(pubspec={pubspec.exists()}, main.dart={main_dart.exists()})"
        )
    if paths.flutter_app.exists():
        shutil.rmtree(paths.flutter_app)
    shutil.move(str(flutter_app), str(paths.flutter_app))
    bound.info("codegen_tasks.done", flutter_app=str(paths.flutter_app), tasks=len(ordered))
    return paths.flutter_app


def _run_screen_layer(
    paths: RunPaths,
    screen_tasks: list[dict[str, object]],
    flutter_app: Path,
    *,
    total: int,
    index_of: dict[str, int],
    done: set[str],
    no_ads: bool,
    diverge_content: bool,
    timeout: int,
    max_attempts: int,
    strict: bool,
    max_parallel: int,
    on_task: Callable[[int, int, str, str, str, int], None] | None,
    on_task_done: Callable[[str], None] | None,
    bound: structlog.stdlib.BoundLogger,
) -> None:
    """Build a layer of independent screen tasks concurrently in git worktrees."""
    _git_base_commit(flutter_app)
    results: dict[str, tuple[str, bool]] = {}
    with ThreadPoolExecutor(max_workers=min(max_parallel, len(screen_tasks))) as pool:
        futures = {
            pool.submit(
                _build_screen_in_worktree,
                paths,
                task,
                flutter_app,
                index=index_of[str(task["id"])],
                total=total,
                no_ads=no_ads,
                diverge_content=diverge_content,
                timeout=timeout,
                max_attempts=max_attempts,
                on_task=on_task,
                tlog=bound.bind(task_id=str(task["id"]), task_type="screen"),
            ): str(task["id"])
            for task in screen_tasks
        }
        for future in as_completed(futures):
            tid = futures[future]
            try:
                results[tid] = future.result()
            except Exception as exc:  # noqa: BLE001 — isolate one worker's failure
                bound.warning("codegen_tasks.screen.worker_error", task_id=tid, error=str(exc))
                results[tid] = (f"task/{_slug(tid)}", False)

    failed: list[str] = []
    for task in screen_tasks:
        tid = str(task["id"])
        branch, ok = results.get(tid, (f"task/{_slug(tid)}", False))
        if ok:
            _merge_screen(flutter_app, branch, _owned_paths(task))
            done.add(tid)
            if on_task_done is not None:
                on_task_done(tid)
        else:
            failed.append(tid)
    _cleanup_worktrees(paths, flutter_app)
    if failed and strict:
        raise RuntimeError(f"codegen screen tasks failed permanently: {failed}")


def build_check(flutter_app: Path) -> bool:
    """Best-effort `flutter analyze` (build sanity). Non-fatal; logs and returns ok."""
    flutter = "/opt/flutter/bin/flutter"
    if not Path(flutter).exists():
        log.warning("claude_gen.flutter_missing", path=flutter)
        return False
    subprocess.run(
        [flutter, "pub", "get"], cwd=flutter_app, capture_output=True, text=True, check=False
    )
    res = subprocess.run(
        [flutter, "analyze"], cwd=flutter_app, capture_output=True, text=True, check=False
    )
    ok = res.returncode == 0
    log.info("claude_gen.build_check", ok=ok, tail=res.stdout[-500:])
    return ok
