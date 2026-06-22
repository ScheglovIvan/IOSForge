"""Hand the crawl output to the local Claude Code CLI and collect a Flutter app.

Prepares claude_ws/ (screenshots + screens.json + PROMPT.md), invokes `claude -p`
non-interactively in that dir, then verifies/moves the generated flutter_app/.
One pass, no 95% loop (phase 2).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from iosforge.common.logging import get_logger
from iosforge.mvp.paths import RunPaths

log = get_logger("mvp.claude_gen")

CLAUDE_BIN = "claude"

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
        cwd=paths.claude_ws, capture_output=True, text=True, timeout=timeout, check=False,
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
