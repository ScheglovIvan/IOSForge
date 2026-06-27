"""Hand the crawl output to the local Claude Code CLI and collect a Flutter app.

Prepares claude_ws/ (screenshots + screens.json + PROMPT.md), invokes `claude -p`
non-interactively in that dir, then verifies/moves the generated flutter_app/.
One pass, no 95% loop (phase 2).
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import structlog

from iosforge.common.logging import get_logger
from iosforge.mvp.analyze import topo_order
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


def _task_prompt(task: dict[str, object]) -> str:
    """Inline per-task prompt for the task runner.

    Inlined to match the existing single-pass prompt; moving prompts to a managed
    PromptSetProvider (SPEC §6) is deferred tech-debt for the MVP vertical.
    """
    screens = task.get("screens", [])
    screen_ids = [str(s) for s in screens] if isinstance(screens, list) else []
    shots = "\n".join(f"- screens/{sid}.png" for sid in screen_ids) or "- (none)"
    return f"""\
You are incrementally building a Flutter app under `flutter_app/` in this directory.

Context files (read as needed):
- `app_spec.json` — the full structured spec for the target app.
- `screens.json` — the raw crawl with element bounds and navigation links.
- `flutter_app/` — the app so far. EDIT IT IN PLACE. Do not delete or rewrite
  files that other tasks created unless this task requires it.

Current task:
- id: {task.get("id")}
- type: {task.get("type")}
- title: {task.get("title")}

Relevant screenshots to LOOK at (vision):
{shots}

Do exactly the work this task describes and nothing more:
- If type is "scaffold": create the Flutter project skeleton —
  `flutter_app/pubspec.yaml`, `flutter_app/lib/main.dart` (app entry + theme +
  routing). Use only the Flutter SDK + material widgets. Keep it compiling.
  The app MUST support deep-link navigation `iosforge://screen/<id>` that routes
  directly to the screen whose id matches `<id>` (the same ids used in
  `app_spec.json` / `screens.json`). Add an `<intent-filter>` with
  `<data android:scheme="iosforge"/>` to `android/app/src/main/AndroidManifest.xml`
  and a router (e.g. `onGenerateRoute` / a platform deep-link handler) that parses
  the incoming URI host/path and shows the matching screen.
- Otherwise: ADD or EDIT files under `flutter_app/lib/` to implement this task,
  reusing the existing scaffold, theme and routing.

Output ONLY changes under `flutter_app/`. Do not run the app.
"""


def run_task(
    paths: RunPaths,
    prompt: str,
    *,
    timeout: int,
    tlog: structlog.stdlib.BoundLogger,
) -> int:
    """Write TASK.md and run one `claude -p` task invocation in the workspace.

    Shared by the Stage D task runner and the Stage E corrective runner. Returns
    the CLI return code; a non-zero code is logged as a warning (best-effort, the
    caller decides whether the resulting flutter_app/ is still valid).
    """
    (paths.claude_ws / "TASK.md").write_text(prompt)
    res = subprocess.run(
        [CLAUDE_BIN, "-p", prompt, "--permission-mode", "acceptEdits"],
        cwd=paths.claude_ws,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if res.returncode != 0:
        tlog.warning("codegen_tasks.task.cli_failed", code=res.returncode, stderr=res.stderr[-500:])
    return res.returncode


def generate_from_tasks(paths: RunPaths, timeout: int = 1800) -> Path:
    """Stage D: build flutter_app/ task-by-task from tasks.json; return its path.

    Parallel to :func:`generate` (single-pass). Executes tasks in dependency
    order, accumulating edits in the persistent workspace flutter_app/.
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
    ordered = topo_order(tasks)
    flutter_app = paths.claude_ws / "flutter_app"
    pubspec = flutter_app / "pubspec.yaml"
    main_dart = flutter_app / "lib" / "main.dart"

    for index, task in enumerate(ordered):
        tlog = bound.bind(task_id=str(task.get("id")), task_type=str(task.get("type")))
        tlog.info("codegen_tasks.task.start", index=index)
        run_task(paths, _task_prompt(task), timeout=timeout, tlog=tlog)
        if index == 0 and not pubspec.exists():
            raise RuntimeError(
                f"scaffold task {task.get('id')!r} did not produce flutter_app/pubspec.yaml"
            )
        tlog.info("codegen_tasks.task.done", index=index)

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
