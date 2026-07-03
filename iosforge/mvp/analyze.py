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

import hashlib
import json
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from iosforge.common.logging import get_logger
from iosforge.mvp import handoff, spec_contract
from iosforge.mvp.paths import RunPaths

log = get_logger("mvp.analyze")

CLAUDE_BIN = "claude"
ANALYZE_TOOLS = "WebSearch WebFetch Read Write Edit Glob Grep"

ANALYZE_PROMPT = """\
You are a senior product analyst reverse-engineering an existing mobile app from
its crawled screens to produce a COMPLETE, build-ready specification. The target
is a native-feeling iOS app; it is tested as a Flutter build. The spec must be
universal — it serves games, photo editors, social, utilities, content-
subscription, e-commerce, productivity and other app types alike.

Inputs in this directory:
- `screens.json` — crawled screens: each has a screenshot path, activity, the
  clickable `elements` (text / resource_id / bounds), and `from`/`tapped`/
  `navigates_to` links showing how screens connect.
- `screens/` — one PNG screenshot per screen. LOOK at every screenshot (vision).

Method:
1. Study every screenshot together with screens.json to understand each screen,
   its components and the navigation graph.
2. Infer the app's identity, domain logic and business model from the UI.
3. Use web search to research how comparable apps in this category are built
   (features, monetization, content, conventions) and cite sources. If web
   access is unavailable, fill `market_research` from your own knowledge and set
   its `sources` to [] and note it in `analysis_quality`.
4. Be explicit about what the crawl could NOT reveal (screens behind login,
   dynamic content) under `analysis_quality`; never invent fake certainty.
5. Frames come from a screen recording; a prior step already separated ad and
   iOS-home frames, so `screens/` holds only real app screens. If a stray non-app
   frame slips through, IGNORE it (do not add it to `screens`). Do NOT reverse-
   engineer ad creatives; instead infer `monetization.ad_placements` (format /
   trigger / screen_context / frequency) from the app's own paywall, rewards and
   "watch ad for coins" screens that ARE present, and keep `ad_networks`
   best-effort (only name a network when on-screen creative/store chrome shows it).

Write a single file `app_spec.json` with this schema (ALL top-level keys are
REQUIRED; use [] / {} / "" when a section does not apply, never omit a key):

{
  "spec_version": "3.0",                // emit exactly this; omit "provenance" (tool injects it)
  "app_name": str,
  "package": str,
  "app_type": str,                      // game | photo-editor | social | utility |
                                        // content-subscription | e-commerce | productivity |
                                        // health | education | media | other
  "one_liner": str,
  "description": str,                   // what it is
  "how_it_works": str,                  // the core user loop, in plain words
  "target_audience": str,
  "platforms": [str],
  "market_research": {
    "similar_apps": [ {"name": str, "notes": str} ],
    "category_conventions": [str],      // expected patterns for this app type
    "monetization_norms": [str],
    "sources": [str]                    // URLs
  },
  "business_logic": {
    "summary": str,
    "domain_rules": [str],              // for games: rules/goal/win-lose/levels/scoring
    "workflows": [ {"name": str, "steps": [str]} ],
    "state_machine": [ {"state": str, "transitions": [str]} ]
  },
  "screens": [
    {
      "id": str, "name": str, "purpose": str, "screenshot": str, "route": str,
      "source": "observed",             // "observed" (seen in a screenshot) | "inferred"
      "components": [ {"type": str, "role": str, "data": str} ],
      "layout_notes": str,
      "states": [str],                  // loading / empty / error / success variants
      "dynamic_content": [str],         // what is data-driven vs static
      "navigates_to": [str]             // screen ids; MUST reference ids in this `screens` array
    }
  ],
  "requirements": [                     // testable, traceable; >=1 item
    {
      "id": "REQ-...",                  // unique, pattern REQ-<slug>
      "type": str,                      // EARS type: ubiquitous | event_driven |
                                        // state_driven | optional_feature | unwanted_behavior
      "text": str,                      // EARS: "When <trigger>, the system shall <action>"
      "priority": str,                  // must|should|could
      "screens": [str],                 // screen ids this requirement touches (must exist)
      "acceptance": [str],              // Given/When/Then style checks
      "source": "observed"              // "observed" | "inferred"
    }
  ],
  "design_tokens": {                    // W3C design tokens ($value/$type/$description)
    "color":   { "primary": {"$value": "#RRGGBB", "$type": "color", "$description": str} },
    "font":    { "body":    {"$value": str, "$type": "fontFamily"} },
    "dimension": { "radius_md": {"$value": "8px", "$type": "dimension"} },
    "dark_mode": bool,
    "ios_adaptation": [str]
  },
  "navigation": {"type": str, "map": [ {"from": str, "to": str, "via": str} ], "deep_links": [str]},
  "content": {
    "data_model": [ {"entity": str, "fields": [str], "relations": [str]} ],
    "content_inventory": [str],         // sticker packs / presets / filters / templates / sounds
    "content_to_seed": [ {"item": str, "amount": str, "format": str, "example": str} ],
    "persistence": str                  // local / cloud / offline / sync
  },
  "monetization": {
    "model": str,                       // free | freemium | subscription | one-time | ads | mixed
    "paywalls": [ {"location": str, "gates": str} ],
    "packages": [ {"name": str, "price": str, "period": str, "includes": [str]} ],
    "ads": [str],                       // legacy free-text summary of the ad strategy
    "ad_networks": [ {"name": str, "confidence": "high|medium|low",
                      "evidence": str, "source": str} ],   // best-effort from creative only
    "ad_placements": [ {   // where/when ads show (infer from kept rewards/paywall screens)
      "format": "banner|interstitial|rewarded|native|offerwall",
      "trigger": str, "screen_context": str, "frequency": str, "frames": [str]
    } ],
    "free_vs_premium": [ {"feature": str, "tier": str} ]
  },
  "backend": {
    "backend_needed": bool, "why": str,
    "admin_panel_needed": bool, "admin_scope": [str],
    "auth": {"required": bool, "methods": [str]},
    "user_roles": [str], "apis": [str], "push_notifications": bool, "cloud_sync": bool
  },
  "permissions": [ {"permission": str, "reason": str} ],
  "integrations": [ {"name": str, "purpose": str} ],
  "cross_cutting": {
    "localization": [str], "onboarding": str, "analytics_events": [str],
    "accessibility": [str], "legal": [str]
  },
  "analysis_quality": {
    "assumptions": [str], "open_questions": [str], "coverage_gaps": [str],
    "confidence": {"overall": str, "by_section": [ {"section": str, "level": str} ]}
  },
  "acceptance_criteria": [str]          // checklist: the full app is done when ...
}

Rules:
- `screens` MUST be non-empty and cover every distinct crawled screen; keep ids
  consistent with screens.json. Every `navigates_to` / requirement `screens`
  entry MUST reference an id that exists in the `screens` array.
- `requirements` MUST be EARS-phrased, each with a unique `REQ-<slug>` id and at
  least one acceptance check; this is what downstream tests are generated from.
- Tag every screen and requirement `source` as "observed" (directly visible in a
  screenshot) or "inferred" (a category convention you added); record inferences
  in `analysis_quality.assumptions`.
- `design_tokens` MUST use the W3C token shape ($value / $type / $description).
- Fill every section as completely as the evidence allows; prefer category
  conventions over leaving a section empty.

Output ONLY the file `app_spec.json`. Do not generate any app code or other files.
"""

DECOMPOSE_PROMPT = """\
You are turning an app specification into an ordered build plan for a Flutter
generator.

Input in this directory:
- `app_spec.json` — the App Spec v3 (screens, requirements, design_tokens,
  navigation, content, monetization, backend, ...).

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


def _run_claude(prompt: str, workdir: Path, timeout: int, tools: str | None = None) -> None:
    """Invoke the local Claude Code CLI non-interactively in ``workdir``."""
    cmd = [CLAUDE_BIN, "-p", prompt, "--permission-mode", "acceptEdits"]
    if tools:
        cmd += ["--allowed-tools", tools]
    res = subprocess.run(
        cmd,
        cwd=workdir,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if res.returncode != 0:
        log.error("analyze.cli_failed", code=res.returncode, stderr=res.stderr[-2000:])
        raise RuntimeError(f"claude CLI exited {res.returncode}: {res.stderr[-500:]}")


_SECTION_TITLES = {
    "one_liner": "Summary",
    "description": "What it is",
    "how_it_works": "How it works",
    "app_type": "App type",
    "target_audience": "Target audience",
    "platforms": "Platforms",
    "market_research": "Market research",
    "business_logic": "Business & domain logic",
    "screens": "Screens",
    "requirements": "Requirements (EARS)",
    "design_tokens": "Design tokens",
    "navigation": "Navigation",
    "content": "Content & data",
    "monetization": "Monetization",
    "backend": "Backend / admin / accounts",
    "permissions": "Permissions",
    "integrations": "Integrations",
    "cross_cutting": "Cross-cutting",
    "analysis_quality": "Analysis quality & gaps",
    "acceptance_criteria": "Acceptance criteria",
}

_SPEC_MD_ORDER = (
    "one_liner",
    "description",
    "how_it_works",
    "app_type",
    "target_audience",
    "platforms",
    "market_research",
    "business_logic",
    "screens",
    "requirements",
    "design_tokens",
    "navigation",
    "content",
    "monetization",
    "backend",
    "permissions",
    "integrations",
    "cross_cutting",
    "analysis_quality",
    "acceptance_criteria",
)


def _scalar(value: object) -> str:
    if isinstance(value, bool):
        return "yes" if value else "no"
    return str(value)


def _md_block(value: object, indent: int = 0) -> list[str]:
    """Render an app_spec value as readable markdown lines."""
    pad = "  " * indent
    lines: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(item, (dict, list)):
                lines.append(f"{pad}- **{key}**:")
                lines.extend(_md_block(item, indent + 1))
            else:
                lines.append(f"{pad}- **{key}**: {_scalar(item)}")
    elif isinstance(value, list):
        for item in value:
            if isinstance(item, dict):
                inline = ", ".join(
                    f"{k}: {_scalar(v)}" for k, v in item.items() if not isinstance(v, (dict, list))
                )
                lines.append(f"{pad}- {inline}" if inline else f"{pad}-")
                for k, v in item.items():
                    if isinstance(v, (dict, list)):
                        lines.append(f"{pad}  - **{k}**:")
                        lines.extend(_md_block(v, indent + 2))
            elif isinstance(item, list):
                lines.extend(_md_block(item, indent + 1))
            else:
                lines.append(f"{pad}- {_scalar(item)}")
    elif isinstance(value, str):
        if value.strip():
            lines.append(f"{pad}{value}")
    else:
        lines.append(f"{pad}{_scalar(value)}")
    return lines


def _render_spec_md(spec: dict[str, object]) -> str:
    """Render a human-readable SPEC.md from the validated app_spec.json."""
    name = spec.get("app_name", "App")
    out: list[str] = [f"# {name} — App Specification", ""]
    for key in _SPEC_MD_ORDER:
        if key not in spec:
            continue
        out.append(f"## {_SECTION_TITLES.get(key, key)}")
        body = _md_block(spec[key])
        out.extend(body if body else ["_(none)_"])
        out.append("")
    return "\n".join(out)


def _crawl_screen_ids(screens_json: Path) -> set[str]:
    try:
        data = json.loads(screens_json.read_text())
    except (OSError, json.JSONDecodeError):
        return set()
    screens = data.get("screens", []) if isinstance(data, dict) else []
    return {str(s["id"]) for s in screens if isinstance(s, dict) and "id" in s}


def analyze(paths: RunPaths, timeout: int = 1800) -> Path:
    """Stage B: derive a validated app_spec.json (+ SPEC.md) from the crawl.

    Runs the local Claude CLI (vision + web research), injects provenance and
    ``spec_version``, then enforces the App Spec v3 contract
    (:func:`iosforge.mvp.spec_contract.validate_spec`) before persisting.
    """
    bound = log.bind(stage="analyze", run_dir=str(paths.run_dir))
    _prepare_analysis_workspace(paths)
    bound.info("analyze.invoking", workdir=str(paths.claude_ws))
    _run_claude(ANALYZE_PROMPT, paths.claude_ws, timeout, tools=ANALYZE_TOOLS)

    produced = paths.claude_ws / "app_spec.json"
    if not produced.exists():
        raise RuntimeError("Claude did not produce app_spec.json")
    try:
        payload = json.loads(produced.read_text())
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"app_spec.json is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("app_spec.json must be a JSON object")

    crawl_ids = _crawl_screen_ids(paths.screens_json)
    source_sha = (
        hashlib.sha256(paths.screens_json.read_bytes()).hexdigest()
        if paths.screens_json.exists()
        else ""
    )
    payload["spec_version"] = spec_contract.SPEC_VERSION
    payload["provenance"] = spec_contract.build_provenance(
        generator="iosforge-mvp-analyze/v3",
        generated_at=datetime.now(UTC).isoformat(),
        source_crawl_sha256=source_sha,
        screen_count=len(crawl_ids),
    )

    spec = spec_contract.validate_spec(payload)
    uncovered = spec_contract.uncovered_screens(spec, crawl_ids)
    if uncovered:
        bound.warning("analyze.coverage_gap", uncovered=uncovered, crawled=len(crawl_ids))

    paths.app_spec_json.write_text(json.dumps(spec, indent=2, ensure_ascii=False))
    produced.unlink(missing_ok=True)
    paths.spec_md.write_text(_render_spec_md(spec))
    handoff_files = handoff.build_handoff(spec, paths.handoff_dir)
    screens = spec["screens"]
    n_screens = len(screens) if isinstance(screens, list) else 0
    bound.info(
        "analyze.done",
        app_spec=str(paths.app_spec_json),
        spec_md=str(paths.spec_md),
        handoff=str(paths.handoff_dir),
        handoff_files=len(handoff_files),
        screens=n_screens,
        uncovered=len(uncovered),
    )
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
