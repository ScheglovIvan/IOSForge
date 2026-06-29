"""Stage 3 codegen orchestrated by Hermes Agent, executed by the local Claude CLI.

Variant A (confirmed with the user): **Hermes orchestrates, the local `claude`
CLI codes** — both on subscription, no Anthropic API token. Hermes runs headless
(`hermes -z`, provider/model from ~/.hermes/config.yaml = Nous Portal), reads the
staged contract (CONSTITUTION.md + app_spec.json + tasks.json + handoff/ +
screens/) and builds ``flutter_app/`` by delegating each task to
``claude -p ... --permission-mode acceptEdits``.

Parallel to :func:`iosforge.mvp.claude_gen.generate_from_tasks` (the single-CLI
path); selected via ``settings.codegen_orchestrator``. Prompts are inlined to
match the rest of the MVP vertical (PromptSetProvider migration is deferred).
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from iosforge.common.logging import get_logger
from iosforge.mvp import constitution
from iosforge.mvp.paths import RunPaths

log = get_logger("mvp.hermes_codegen")

HERMES_BIN = "hermes"

ORCHESTRATION_PROMPT = """\
You are the BUILD ORCHESTRATOR for a Flutter app. Everything you need is in this
working directory. Read and obey CONSTITUTION.md first — it is binding.

Files here:
- CONSTITUTION.md  — binding rules (stack, fidelity, routing, process). FOLLOW IT.
- app_spec.json    — full structured spec (screens, requirements, content, backend).
- tasks.json       — ordered, acyclic build plan (dependency graph).
- handoff/         — requirements.md, design.md, tokens.json, features/acceptance.feature,
                     traceability.md (and openapi.yaml if a backend is needed).
- screens/         — one PNG per target screen. The app must match these 1:1 (iOS-priority).

How to build:
1. Build flutter_app/ by executing tasks.json in dependency order.
2. The ACTUAL coding of each task MUST be delegated to the local Claude Code CLI:
   run `claude -p '<precise task instruction naming the screens/*.png to match and the
   REQ-* requirements to satisfy>' --permission-mode acceptEdits` from this directory.
   Do not write app code yourself — orchestrate claude.
3. Scaffold + theme(from handoff/tokens.json) + go_router(routes from the nav graph) run
   FIRST and serially. Independent screen tasks MAY run in parallel, each in its own git
   worktree, then merge. Then data/state, then integration, then content + iOS adaptation.
4. Include codemagic.yaml + iOS config (Info.plist, bundle id) per CONSTITUTION.md.

Process (lean): verify at MILESTONES only (after scaffold, after screens, final) by
running `flutter analyze` and having claude fix errors — NOT after every change.

Done = flutter_app/ contains pubspec.yaml and lib/main.dart and analyzes cleanly.
Output only files under flutter_app/.
"""


def _prepare_hermes_workspace(paths: RunPaths) -> None:
    """Stage the full contract (constitution + spec + tasks + handoff + screens)."""
    ws = paths.claude_ws
    ws.mkdir(parents=True, exist_ok=True)

    ws_screens = ws / "screens"
    if ws_screens.exists():
        shutil.rmtree(ws_screens)
    shutil.copytree(paths.screens_dir, ws_screens)
    shutil.copy2(paths.screens_json, ws / "screens.json")
    shutil.copy2(paths.app_spec_json, ws / "app_spec.json")
    shutil.copy2(paths.tasks_json, ws / "tasks.json")

    ws_handoff = ws / "handoff"
    if ws_handoff.exists():
        shutil.rmtree(ws_handoff)
    if paths.handoff_dir.exists():
        shutil.copytree(paths.handoff_dir, ws_handoff)

    spec = json.loads(paths.app_spec_json.read_text())
    (ws / "CONSTITUTION.md").write_text(constitution.render_constitution(spec))
    (ws / "AGENTS.md").write_text(
        "Build orchestration workspace. Read CONSTITUTION.md (binding), then "
        "app_spec.json, tasks.json and handoff/. Delegate coding to the local "
        "`claude` CLI; build the app under flutter_app/.\n"
    )
    (ws / "ORCHESTRATION_PROMPT.md").write_text(ORCHESTRATION_PROMPT)


def _run_hermes(prompt: str, workdir: Path, timeout: int, toolset: str = "coding") -> int:
    """Invoke Hermes headless (one-shot) in ``workdir``; return its exit code."""
    res = subprocess.run(
        [HERMES_BIN, "-z", prompt, "-t", toolset, "--yolo"],
        cwd=workdir,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if res.returncode != 0:
        log.error("hermes_codegen.cli_failed", code=res.returncode, stderr=res.stderr[-2000:])
    return res.returncode


def generate_via_hermes(paths: RunPaths, timeout: int = 7200) -> Path:
    """Stage 3 (Variant A): Hermes orchestrates the Flutter build, claude codes it.

    Requires app_spec.json + tasks.json (run analyze + decompose first). Returns
    the path to the produced ``flutter_app/``.
    """
    bound = log.bind(stage="hermes_codegen", run_dir=str(paths.run_dir))
    if not paths.app_spec_json.exists():
        raise RuntimeError(f"app_spec.json missing; run analyze first: {paths.app_spec_json}")
    if not paths.tasks_json.exists():
        raise RuntimeError(f"tasks.json missing; run decompose first: {paths.tasks_json}")

    _prepare_hermes_workspace(paths)
    bound.info("hermes_codegen.invoking", workdir=str(paths.claude_ws))
    code = _run_hermes(ORCHESTRATION_PROMPT, paths.claude_ws, timeout)

    generated = paths.claude_ws / "flutter_app"
    pubspec = generated / "pubspec.yaml"
    main_dart = generated / "lib" / "main.dart"
    if not (pubspec.exists() and main_dart.exists()):
        raise RuntimeError(
            "Hermes orchestration did not produce a valid flutter_app/ "
            f"(exit={code}, pubspec={pubspec.exists()}, main.dart={main_dart.exists()})"
        )
    if paths.flutter_app.exists():
        shutil.rmtree(paths.flutter_app)
    shutil.move(str(generated), str(paths.flutter_app))
    bound.info("hermes_codegen.done", flutter_app=str(paths.flutter_app), exit=code)
    return paths.flutter_app
