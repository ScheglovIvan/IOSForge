"""Stage 3 codegen via an agentic orchestrator that owns the same staged contract.

Two interchangeable orchestrator backends share one workspace contract
(CONSTITUTION.md + app_spec.json + tasks.json + handoff/ + screens/) and one
validation/move of the produced ``flutter_app/``; they differ only in *who*
orchestrates the build:

- **Hermes** (Variant A, ``codegen_orchestrator="hermes"``): Hermes Agent runs
  headless (``hermes -z``, Nous Portal brain) and delegates the coding of each
  task to the local ``claude -p ... --permission-mode acceptEdits`` executor.
- **Claude orchestrator** (``codegen_orchestrator="cloud"``): the local Claude
  Code agent IS the orchestrator and builds ``flutter_app/`` directly, optionally
  fanning out to parallel screen subagents. No nested ``claude -p`` executor.

Both are parallel to :func:`iosforge.mvp.claude_gen.generate_from_tasks` (the
single-pass task runner, ``codegen_orchestrator="claude"``); the active backend
is selected by :func:`iosforge.mvp.codegen.generate`. Prompts are inlined to
match the rest of the MVP vertical (PromptSetProvider migration is deferred).
"""

from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

from iosforge.common.logging import get_logger
from iosforge.mvp import constitution
from iosforge.mvp.analyze import stage_archive_context
from iosforge.mvp.paths import RunPaths

log = get_logger("mvp.hermes_codegen")

HERMES_BIN = "hermes"
CLAUDE_BIN = "claude"


def _orchestration_prompt(max_parallel: int) -> str:
    return f"""\
You are the BUILD ORCHESTRATOR for a Flutter app. Everything you need is in this
working directory. Read and obey CONSTITUTION.md first — it is binding.

Files here:
- CONSTITUTION.md  — binding rules (stack, fidelity, routing, parallel discipline). FOLLOW IT.
- app_spec.json    — full structured spec (screens, requirements, content, backend).
- tasks.json       — ordered, acyclic build plan (dependency graph).
- handoff/         — requirements.md, design.md, tokens.json, features/acceptance.feature,
                     traceability.md (and openapi.yaml if a backend is needed).
- screens/         — one PNG per target screen. The app must match these 1:1 (iOS-priority).
- source/<id>.json (OPTIONAL) — EXACT native view hierarchy per screen (frame geometry,
                     font postscript_name/size/weight, #RRGGBBAA colors, layer props,
                     asset_ref.media_id). GROUND TRUTH — prefer over eyeballing the PNG.
- fonts.json / fonts/   (OPTIONAL) — real fonts; bundle the `.ttf`s into assets + pubspec and
                     set the theme fontFamily to the primary custom family.
- media.json / media/   (OPTIONAL) — real media (id/role/kind/path). Embed by node
                     asset_ref.media_id; for SwiftUI splash/paywall backgrounds pick by `role`
                     (splash_background/paywall_hero) and use as the screen background layer.

How to build:
1. Execute tasks.json in dependency order to build flutter_app/.
2. The ACTUAL coding of each task MUST be delegated to the local coding CLI:
   run `claude -p '<precise task instruction naming the screens/*.png to match and the
   REQ-* requirements to satisfy>' --permission-mode acceptEdits` from this directory.
   Do not write app code yourself — orchestrate the executor.
3. PHASES:
   a. FOUNDATION (serial): scaffold + theme (from handoff/tokens.json) + go_router with
      EVERY route pre-registered to a placeholder widget (so screens never touch the router).
   b. SCREENS (PARALLEL): build independent screen tasks concurrently — up to {max_parallel}
      at once — each in its OWN git worktree, then merge. Per CONSTITUTION.md a screen worker
      edits ONLY its `lib/features/<screen>/`; never the shared core. This is the speed win.
   c. INTEGRATION (serial): data/state, wiring, content seeding, iOS adaptation.
4. Include codemagic.yaml + iOS config (Info.plist, bundle id) per CONSTITUTION.md.

Process (lean): verify at MILESTONES only (after foundation, after screens, final) by
running `flutter analyze` and having the executor fix errors — NOT after every change.

Done = flutter_app/ contains pubspec.yaml and lib/main.dart and analyzes cleanly.
Output only files under flutter_app/.
"""


def _claude_orchestration_prompt(max_parallel: int) -> str:
    return f"""\
You are the BUILD ORCHESTRATOR and BUILDER for a Flutter app. Everything you need
is in this working directory. Read and obey CONSTITUTION.md first — it is binding.

Files here:
- CONSTITUTION.md  — binding rules (stack, fidelity, routing, parallel discipline). FOLLOW IT.
- app_spec.json    — full structured spec (screens, requirements, content, backend).
- tasks.json       — ordered, acyclic build plan (dependency graph).
- handoff/         — requirements.md, design.md, tokens.json, features/acceptance.feature,
                     traceability.md (and openapi.yaml if a backend is needed).
- screens/         — one PNG per target screen. The app must match these 1:1 (iOS-priority).
- source/<id>.json (OPTIONAL) — EXACT native view hierarchy per screen (frame geometry,
                     font postscript_name/size/weight, #RRGGBBAA colors, layer props,
                     asset_ref.media_id). GROUND TRUTH — prefer over eyeballing the PNG.
- fonts.json / fonts/   (OPTIONAL) — real fonts; bundle the `.ttf`s into assets + pubspec and
                     set the theme fontFamily to the primary custom family.
- media.json / media/   (OPTIONAL) — real media (id/role/kind/path). Embed by node
                     asset_ref.media_id; for SwiftUI splash/paywall backgrounds pick by `role`
                     (splash_background/paywall_hero) and use as the screen background layer.

How to build:
1. Execute tasks.json in dependency order to build flutter_app/. You build the code
   yourself with your own file-editing tools — do NOT shell out to another `claude`
   CLI; YOU are the executor.
2. PHASES:
   a. FOUNDATION (serial): scaffold + theme (from handoff/tokens.json) + go_router with
      EVERY route pre-registered to a placeholder widget (so screens never touch the router).
   b. SCREENS (PARALLEL): build independent screen tasks concurrently — dispatch
      up to {max_parallel} parallel subagents at once (Task tool), each owning ONE screen
      and editing ONLY its `lib/features/<screen>/` per CONSTITUTION.md; never shared core.
   c. INTEGRATION (serial): data/state, wiring, content seeding, iOS adaptation.
3. Include codemagic.yaml + iOS config (Info.plist, bundle id) per CONSTITUTION.md.

Process (lean): verify at MILESTONES only (after foundation, after screens, final) by
running `flutter analyze` and fixing errors — NOT after every change.

Done = flutter_app/ contains pubspec.yaml and lib/main.dart and analyzes cleanly.
Output only files under flutter_app/.
"""


def _prepare_hermes_workspace(paths: RunPaths, orchestration_prompt: str, agents_md: str) -> None:
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

    stage_archive_context(paths, ws, include_bytes=True)

    spec = json.loads(paths.app_spec_json.read_text())
    (ws / "CONSTITUTION.md").write_text(constitution.render_constitution(spec))
    (ws / "AGENTS.md").write_text(agents_md)
    (ws / "ORCHESTRATION_PROMPT.md").write_text(orchestration_prompt)


_HERMES_AGENTS_MD = (
    "Build orchestration workspace. Read CONSTITUTION.md (binding), then "
    "app_spec.json, tasks.json and handoff/. Delegate coding to the local "
    "`claude` CLI; build the app under flutter_app/.\n"
)

_CLAUDE_AGENTS_MD = (
    "Build orchestration workspace. Read CONSTITUTION.md (binding), then "
    "app_spec.json, tasks.json and handoff/. Build the app under flutter_app/ "
    "yourself; fan out to parallel screen subagents for the screen layer.\n"
)


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


def _run_claude(prompt: str, workdir: Path, timeout: int) -> int:
    """Invoke the local Claude Code agent headless in ``workdir``; return its exit code."""
    res = subprocess.run(
        [CLAUDE_BIN, "-p", prompt, "--permission-mode", "acceptEdits"],
        cwd=workdir,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if res.returncode != 0:
        log.error("hermes_codegen.cli_failed", code=res.returncode, stderr=res.stderr[-2000:])
    return res.returncode


def _generate_via_orchestrator(
    paths: RunPaths,
    *,
    orchestrator: str,
    prompt: str,
    agents_md: str,
    runner: Callable[[str, Path, int], int],
    timeout: int,
    max_parallel: int,
) -> Path:
    """Stage the contract, run ``runner`` as the orchestrator, validate flutter_app/."""
    bound = log.bind(stage="hermes_codegen", run_dir=str(paths.run_dir), orchestrator=orchestrator)
    if not paths.app_spec_json.exists():
        raise RuntimeError(f"app_spec.json missing; run analyze first: {paths.app_spec_json}")
    if not paths.tasks_json.exists():
        raise RuntimeError(f"tasks.json missing; run decompose first: {paths.tasks_json}")

    _prepare_hermes_workspace(paths, prompt, agents_md)
    bound.info("hermes_codegen.invoking", workdir=str(paths.claude_ws), max_parallel=max_parallel)
    code = runner(prompt, paths.claude_ws, timeout)

    generated = paths.claude_ws / "flutter_app"
    pubspec = generated / "pubspec.yaml"
    main_dart = generated / "lib" / "main.dart"
    if not (pubspec.exists() and main_dart.exists()):
        raise RuntimeError(
            f"{orchestrator} orchestration did not produce a valid flutter_app/ "
            f"(exit={code}, pubspec={pubspec.exists()}, main.dart={main_dart.exists()})"
        )
    if paths.flutter_app.exists():
        shutil.rmtree(paths.flutter_app)
    shutil.move(str(generated), str(paths.flutter_app))
    bound.info("hermes_codegen.done", flutter_app=str(paths.flutter_app), exit=code)
    return paths.flutter_app


def generate_via_hermes(paths: RunPaths, timeout: int = 7200, max_parallel: int = 4) -> Path:
    """Stage 3 (Variant A): Hermes orchestrates the Flutter build, the local CLI codes it.

    The screen layer is built in parallel (worktree-per-task, up to ``max_parallel``
    concurrent workers). Requires app_spec.json + tasks.json (run analyze + decompose
    first). Returns the path to the produced ``flutter_app/``.
    """
    return _generate_via_orchestrator(
        paths,
        orchestrator="Hermes",
        prompt=_orchestration_prompt(max_parallel),
        agents_md=_HERMES_AGENTS_MD,
        runner=_run_hermes,
        timeout=timeout,
        max_parallel=max_parallel,
    )


def generate_via_claude_orchestrator(
    paths: RunPaths, timeout: int = 7200, max_parallel: int = 4
) -> Path:
    """Stage 3: the local Claude Code agent orchestrates AND builds flutter_app/ directly.

    A drop-in replacement for :func:`generate_via_hermes` that swaps the Hermes
    orchestrator brain for the local Claude agent; it builds the screen layer with
    up to ``max_parallel`` parallel subagents instead of nested ``claude -p`` calls.
    Requires app_spec.json + tasks.json. Returns the produced ``flutter_app/``.
    """
    return _generate_via_orchestrator(
        paths,
        orchestrator="Claude",
        prompt=_claude_orchestration_prompt(max_parallel),
        agents_md=_CLAUDE_AGENTS_MD,
        runner=_run_claude,
        timeout=timeout,
        max_parallel=max_parallel,
    )
