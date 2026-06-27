"""Analysis (Stage B) and decomposition (Stage C) of the crawl output.

These two steps sit between the emulator crawl and the single-pass codegen in
:mod:`iosforge.mvp.claude_gen`. They do not replace or modify that path; they
produce richer intermediate artifacts (``app_spec.json``, ``tasks.json``) for a
future multi-pass generator.

Stage B (:func:`analyze`) asks the local Claude Code CLI to read the screenshots
(vision) plus ``screens.json`` and emit a structured ``app_spec.json``.

Stage C (:func:`decompose`) turns that spec into an ordered, acyclic task graph
``tasks.json`` that a codegen loop can execute task by task.

The prompts here are inlined to mirror :mod:`iosforge.mvp.claude_gen`. Moving
them to a managed ``PromptSetProvider`` (SPEC §6, no hardcoded prompts) is
deferred tech-debt for the MVP vertical only.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from iosforge.common.logging import get_logger
from iosforge.mvp.paths import RunPaths

log = get_logger("mvp.analyze")

CLAUDE_BIN = "claude"

ANALYZE_PROMPT = """\
You are analysing an existing Android app from its crawled screens to produce a
structured specification a Flutter generator can build from.

Inputs in this directory:
- `screens.json` — crawled screens: each has a screenshot path, the current
  activity, the clickable `elements` (text / resource_id / bounds), and
  `from`/`tapped` links showing how screens connect.
- `screens/` — one PNG screenshot per screen. LOOK at every screenshot.

Task:
1. Study every PNG screenshot (use vision) together with the screens.json
   structure to understand each screen's purpose, components and navigation.
2. Write a single file `app_spec.json` in this directory with EXACTLY this schema:

{
  "app_name": str,
  "package": str,
  "screens": [
    {
      "id": str,
      "name": str,
      "purpose": str,
      "screenshot": str,
      "components": [ {"type": str} ],
      "layout_notes": str,
      "navigates_to": [str]
    }
  ],
  "flows": [ {"name": str, "steps": [str]} ],
  "data_model": [],
  "design": {"primary_color": str, "theme": str}
}

3. The `screens` array MUST be non-empty and cover every distinct crawled screen.
   Keep ids consistent with screens.json so navigation can be wired later.

Output ONLY the file `app_spec.json`. Do not generate any app code.
"""

DECOMPOSE_PROMPT = """\
You are turning an app specification into an ordered build plan for a Flutter
generator.

Input in this directory:
- `app_spec.json` — the structured spec (screens, flows, data_model, design).

Task:
Write a single file `tasks.json` in this directory with EXACTLY this schema:

{
  "tasks": [
    {
      "id": str,
      "type": "scaffold" | "screen" | "flow" | "state" | "polish",
      "title": str,
      "screens": [str],
      "deps": [str]
    }
  ]
}

Rules:
1. The FIRST task MUST be a single "scaffold" task (Flutter project + theme +
   routing) with `"deps": []`.
2. Every "screen" task MUST depend on the scaffold task.
3. `deps` may only reference ids of tasks defined earlier in the list; the
   dependency graph MUST be acyclic.
4. Every task MUST have a unique non-empty `id`, a `type` from the set above,
   and a `title`.

Output ONLY the file `tasks.json`. Do not generate any app code.
"""


def _prepare_analysis_workspace(paths: RunPaths) -> None:
    """Stage the screenshots, screens.json and the analysis prompt into claude_ws/."""
    paths.claude_ws.mkdir(parents=True, exist_ok=True)
    ws_screens = paths.claude_ws / "screens"
    if ws_screens.exists():
        shutil.rmtree(ws_screens)
    shutil.copytree(paths.screens_dir, ws_screens)
    shutil.copy2(paths.screens_json, paths.claude_ws / "screens.json")
    (paths.claude_ws / "ANALYZE_PROMPT.md").write_text(ANALYZE_PROMPT)


def _prepare_decompose_workspace(paths: RunPaths) -> None:
    """Stage app_spec.json and the decomposition prompt into claude_ws/."""
    paths.claude_ws.mkdir(parents=True, exist_ok=True)
    shutil.copy2(paths.app_spec_json, paths.claude_ws / "app_spec.json")
    (paths.claude_ws / "DECOMPOSE_PROMPT.md").write_text(DECOMPOSE_PROMPT)


def _run_claude(prompt: str, workdir: Path, timeout: int) -> None:
    """Invoke the local Claude Code CLI non-interactively in ``workdir``."""
    res = subprocess.run(
        [CLAUDE_BIN, "-p", prompt, "--permission-mode", "acceptEdits"],
        cwd=workdir,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if res.returncode != 0:
        log.error("analyze.cli_failed", code=res.returncode, stderr=res.stderr[-2000:])
        raise RuntimeError(f"claude CLI exited {res.returncode}: {res.stderr[-500:]}")


def analyze(paths: RunPaths, timeout: int = 1800) -> Path:
    """Stage B: derive a structured app_spec.json from the crawl; return its path."""
    bound = log.bind(stage="analyze", run_dir=str(paths.run_dir))
    _prepare_analysis_workspace(paths)
    bound.info("analyze.invoking", workdir=str(paths.claude_ws))
    _run_claude(ANALYZE_PROMPT, paths.claude_ws, timeout)

    produced = paths.claude_ws / "app_spec.json"
    if not produced.exists():
        raise RuntimeError("Claude did not produce app_spec.json")
    try:
        spec = json.loads(produced.read_text())
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"app_spec.json is not valid JSON: {exc}") from exc
    screens = spec.get("screens") if isinstance(spec, dict) else None
    if not isinstance(screens, list) or not screens:
        raise RuntimeError("app_spec.json has an empty or missing 'screens' array")

    shutil.move(str(produced), str(paths.app_spec_json))
    bound.info("analyze.done", app_spec=str(paths.app_spec_json), screens=len(screens))
    return paths.app_spec_json


def topo_order(tasks: list[dict[str, object]]) -> list[dict[str, object]]:
    """Return tasks in dependency order (deps before dependents).

    Kahn's algorithm. Raises RuntimeError if a task references an unknown
    dependency id or if the graph contains a cycle. Among tasks that become
    ready together the original input order is preserved, so the result is
    deterministic. Shared by the tasks.json acyclic check and the task runner.
    """
    by_id: dict[str, dict[str, object]] = {str(t["id"]): t for t in tasks}
    ids = set(by_id)
    deps: dict[str, set[str]] = {}
    for t in tasks:
        task_deps = t.get("deps", [])
        if not isinstance(task_deps, list):
            raise RuntimeError(f"task {t['id']!r} has non-list deps")
        missing = {str(d) for d in task_deps} - ids
        if missing:
            raise RuntimeError(f"task {t['id']!r} depends on unknown ids: {sorted(missing)}")
        deps[str(t["id"])] = {str(d) for d in task_deps}

    ordered: list[dict[str, object]] = []
    resolved: set[str] = set()
    remaining = [str(t["id"]) for t in tasks]
    while remaining:
        ready = [tid for tid in remaining if deps[tid] <= resolved]
        if not ready:
            raise RuntimeError(
                f"tasks.json dependency graph has a cycle among: {sorted(remaining)}"
            )
        resolved.update(ready)
        ordered.extend(by_id[tid] for tid in ready)
        remaining = [tid for tid in remaining if tid not in resolved]
    return ordered


def _validate_tasks(payload: object) -> list[dict[str, object]]:
    """Validate the tasks.json structure and return the task list."""
    if not isinstance(payload, dict):
        raise RuntimeError("tasks.json must be a JSON object")
    tasks = payload.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        raise RuntimeError("tasks.json has an empty or missing 'tasks' array")
    seen: set[str] = set()
    for t in tasks:
        if not isinstance(t, dict):
            raise RuntimeError("every task must be a JSON object")
        for field in ("id", "type", "title"):
            value = t.get(field)
            if not isinstance(value, str) or not value:
                raise RuntimeError(f"task is missing a non-empty {field!r}")
        tid = str(t["id"])
        if tid in seen:
            raise RuntimeError(f"duplicate task id {tid!r}")
        seen.add(tid)
    topo_order(tasks)
    return tasks


def decompose(paths: RunPaths, timeout: int = 1800) -> Path:
    """Stage C: turn app_spec.json into an acyclic tasks.json; return its path."""
    bound = log.bind(stage="decompose", run_dir=str(paths.run_dir))
    if not paths.app_spec_json.exists():
        raise RuntimeError(f"app_spec.json missing; run analyze first: {paths.app_spec_json}")
    _prepare_decompose_workspace(paths)
    bound.info("decompose.invoking", workdir=str(paths.claude_ws))
    _run_claude(DECOMPOSE_PROMPT, paths.claude_ws, timeout)

    produced = paths.claude_ws / "tasks.json"
    if not produced.exists():
        raise RuntimeError("Claude did not produce tasks.json")
    try:
        payload = json.loads(produced.read_text())
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"tasks.json is not valid JSON: {exc}") from exc
    tasks = _validate_tasks(payload)

    shutil.move(str(produced), str(paths.tasks_json))
    bound.info("decompose.done", tasks_json=str(paths.tasks_json), tasks=len(tasks))
    return paths.tasks_json
