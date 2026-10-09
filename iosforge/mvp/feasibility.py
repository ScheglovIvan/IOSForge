"""SCOPE stage: propose an MVP scope + iOS feasibility, then prune the spec.

Two halves (see DECISIONS 2026-10-09 "Phase 2: scope-гейт"):

* :func:`propose` is model-assisted — it hands the App Spec v3 to the local
  ``claude`` CLI and turns the reply into a :class:`ScopeDecision` with
  ``status="proposed"``. It mirrors the subprocess pattern of
  :mod:`iosforge.mvp.analyze` / :mod:`iosforge.mvp.claude_gen` (``claude -p`` in a
  staged workdir, read back a produced JSON file).
* :func:`apply_scope` is deterministic (no model): it trims ``app_spec.screens``
  to the included set and cleans the dangling references left behind, exactly as
  :func:`iosforge.mvp.screen_filter.filter_screens` cleans ``screens.json``, then
  re-validates against the App Spec v3 contract.

The ``SCOPE_PROMPT`` is inlined here rather than loaded from
``providers/promptset`` — accepted tech debt for the ``mvp`` vertical (SPEC §6;
DECISIONS 2026-07-07/2026-07-13 and the Phase 2 entry).
"""

from __future__ import annotations

import json
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from iosforge.common.config import Settings, get_settings
from iosforge.common.logging import get_logger
from iosforge.mvp import spec_contract
from iosforge.mvp.paths import RunPaths
from iosforge.mvp.scope_models import (
    FeasibilityFinding,
    FeasibilityReport,
    ScopeCounts,
    ScopeDecision,
    ScreenScope,
)
from iosforge.storage.client import ArtifactRef, ArtifactStorage, build_key

log = get_logger("mvp.feasibility")

CLAUDE_BIN = "claude"
SCOPE_TOOLS = "Read Write Glob"
SCOPE_KIND = "scope"
SCOPE_NAME = "scope.json"

SCOPE_PROMPT = """\
You are planning the MVP scope for a native iOS (SwiftUI) clone of an existing app.

Inputs in this directory:
- `app_spec.json` — the App Spec v3 for the app. Its `screens` array lists every
  screen: `id`, `name`, `purpose`, `navigates_to` (ids this screen links to) and,
  optionally, `state_of` (the screen is only a state of that base screen).
- `screens/` — one PNG per original screen (vision aid), named by `screenshot`.

Decide TWO things:
1. SCOPE — which screens belong to the MVP CORE versus which can be dropped.
   Onboarding walkthroughs, help/support, FAQ, "about", legal/settings filler and
   duplicate explainer screens are usually NOT core. The real product surface
   (the screens users return to) IS core. Keep navigation coherent: do not drop a
   screen that every core flow must pass through. Decide a `state_of` screen
   together with its base screen (a state without its base is meaningless).
2. FEASIBILITY — how well each needed capability maps onto NATIVE iOS. Flag
   capabilities (e.g. "screen recording", "HealthKit", "background location",
   "home-screen widgets", "live streaming") as `native` (public API exists),
   `partial` (approximate with public API) or `blocked` (no public API). This is
   ADVISORY — it informs the operator, it does not force a screen out.

Output ONLY a file `scope_proposal.json` in this directory:

{
  "scope_mode": "core",
  "screens": [
    {"screen_id": "<id>", "name": "<name>", "include": true,
     "reason": "<why core / why dropped>", "group": "onboarding|core|support|settings|legal|other"}
  ],
  "core_flows": ["<short name of a flow that defines the MVP>"],
  "feasibility": {
    "overall_verdict": "native|partial|blocked",
    "summary": "<one paragraph>",
    "findings": [
      {"capability": "<name>", "verdict": "native|partial|blocked",
       "note": "<short>", "screens": ["<affected screen id>"]}
    ]
  },
  "notes": "<optional operator-facing note>"
}

Rules:
- Include EVERY screen id from `app_spec.json` exactly once, with an `include` flag.
- Use the exact screen ids from `app_spec.json`.
- Prefer a lean core: a smaller, coherent MVP is better than cloning everything.
Output nothing but `scope_proposal.json`.
"""


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
        log.error("feasibility.cli_failed", code=res.returncode, stderr=res.stderr[-2000:])
        raise RuntimeError(f"claude CLI exited {res.returncode}: {res.stderr[-500:]}")


def _scope_workspace(paths: RunPaths) -> Path:
    """Stage app_spec.json + screenshots + the scope prompt into a fresh workdir."""
    ws = paths.run_dir / "scope_ws"
    ws.mkdir(parents=True, exist_ok=True)
    shutil.copy2(paths.app_spec_json, ws / "app_spec.json")
    ws_screens = ws / "screens"
    if ws_screens.exists():
        shutil.rmtree(ws_screens)
    if paths.screens_dir.exists():
        shutil.copytree(paths.screens_dir, ws_screens)
    (ws / "SCOPE_PROMPT.md").write_text(SCOPE_PROMPT)
    return ws


def _screen_names(spec: dict[str, Any]) -> dict[str, str]:
    names: dict[str, str] = {}
    for screen in spec.get("screens", []):
        if isinstance(screen, dict) and "id" in screen:
            names[str(screen["id"])] = str(screen.get("name", screen["id"]))
    return names


def _parse_proposal(payload: dict[str, Any], spec: dict[str, Any]) -> ScopeDecision:
    names = _screen_names(spec)
    screens: list[ScreenScope] = []
    for row in payload.get("screens", []):
        if not isinstance(row, dict) or "screen_id" not in row:
            continue
        sid = str(row["screen_id"])
        screens.append(
            ScreenScope(
                screen_id=sid,
                name=str(row.get("name") or names.get(sid, sid)),
                include=bool(row.get("include", True)),
                reason=str(row.get("reason", "")),
                group=(str(row["group"]) if row.get("group") else None),
            )
        )
    feas_obj = payload.get("feasibility")
    feas_raw: dict[str, Any] = feas_obj if isinstance(feas_obj, dict) else {}
    findings: list[FeasibilityFinding] = []
    for row in feas_raw.get("findings", []):
        if not isinstance(row, dict):
            continue
        row_screens = row.get("screens")
        findings.append(
            FeasibilityFinding(
                capability=str(row.get("capability", "")),
                verdict=_coerce_verdict(row.get("verdict")),
                note=str(row.get("note", "")),
                screens=[str(s) for s in row_screens] if isinstance(row_screens, list) else [],
            )
        )
    feasibility = FeasibilityReport(
        overall_verdict=_coerce_verdict(feas_raw.get("overall_verdict")),
        summary=str(feas_raw.get("summary", "")),
        findings=findings,
    )
    mode: Literal["core", "full"] = "full" if str(payload.get("scope_mode")) == "full" else "core"
    decision = ScopeDecision(
        status="proposed",
        scope_mode=mode,
        screens=screens,
        core_flows=[str(f) for f in payload.get("core_flows", []) if f],
        feasibility=feasibility,
        counts=ScopeCounts.from_screens(screens),
        notes=str(payload.get("notes", "")),
        proposed_at=datetime.now(UTC),
    )
    return decision


def _coerce_verdict(value: object) -> Literal["native", "partial", "blocked"]:
    text = str(value)
    if text == "native":
        return "native"
    if text == "blocked":
        return "blocked"
    return "partial"


def propose(paths: RunPaths, settings: Settings | None = None) -> ScopeDecision:
    """Model-assisted SCOPE proposal from ``app_spec.json`` → :class:`ScopeDecision`.

    Stages the spec + screenshots into a workdir, runs ``claude -p`` with
    :data:`SCOPE_PROMPT`, reads back ``scope_proposal.json`` and returns a
    ``status="proposed"`` decision. ``counts`` are derived here; MinIO versioning
    carries the proposed/approved history (see :func:`save_scope`).
    """
    settings = settings or get_settings()
    bound = log.bind(stage="scope", run_dir=str(paths.run_dir))
    spec = json.loads(paths.app_spec_json.read_text())
    ws = _scope_workspace(paths)
    bound.info("feasibility.invoking", workdir=str(ws))
    _run_claude(SCOPE_PROMPT, ws, settings.walkthrough_job_timeout_s, tools=SCOPE_TOOLS)

    produced = ws / "scope_proposal.json"
    if not produced.exists():
        raise RuntimeError("Claude did not produce scope_proposal.json")
    try:
        payload = json.loads(produced.read_text())
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"scope_proposal.json is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("scope_proposal.json must be a JSON object")

    decision = _parse_proposal(payload, spec)
    paths.scope_json.write_text(decision.model_dump_json(indent=2))
    bound.info(
        "feasibility.proposed",
        total=decision.counts.total,
        included=decision.counts.included,
        mode=decision.scope_mode,
        verdict=decision.feasibility.overall_verdict,
    )
    return decision


def _edge_ok(edge: dict[str, Any], included: set[str]) -> bool:
    for side in ("from", "to"):
        value = edge.get(side)
        if value and str(value) not in included:
            return False
    return True


def apply_scope(paths: RunPaths, scope: ScopeDecision | None = None) -> None:
    """Deterministically prune ``app_spec.json`` to the approved scope (no model).

    When ``scope`` is ``None`` the decision is read from ``paths.scope_json`` (the
    caller is expected to have hydrated the approved version from MinIO via
    :func:`load_scope`). ``scope_mode == "full"`` and a missing/empty scope are
    no-ops. Otherwise the spec keeps only ``include`` screens and drops the
    dangling references they leave behind (``navigates_to``, ``navigation.map``
    edges, ``navigation.tabs`` roots, ``state_of`` links to a dropped base screen,
    ``requirements[].screens``, ``screen_count``) — mirroring
    :func:`iosforge.mvp.screen_filter.filter_screens` — then re-validates against
    the App Spec v3 contract.
    """
    if scope is None:
        if not paths.scope_json.exists():
            return
        scope = ScopeDecision.model_validate_json(paths.scope_json.read_text())
    if scope.scope_mode == "full":
        return

    included = {s.screen_id for s in scope.screens if s.include}
    spec = json.loads(paths.app_spec_json.read_text())
    screens = spec.get("screens", []) if isinstance(spec, dict) else []
    kept = [s for s in screens if isinstance(s, dict) and str(s.get("id")) in included]
    if not kept:
        log.warning("feasibility.apply_scope.empty_keeping_all", total=len(screens))
        return

    for screen in kept:
        nav = screen.get("navigates_to")
        if isinstance(nav, list):
            screen["navigates_to"] = [n for n in nav if str(n) in included]
        if "state_of" in screen and str(screen["state_of"]) not in included:
            log.warning(
                "feasibility.apply_scope.orphaned_state",
                screen_id=screen.get("id"),
                state_of=screen.pop("state_of"),
            )
    spec["screens"] = kept

    navigation = spec.get("navigation")
    if isinstance(navigation, dict) and isinstance(navigation.get("map"), list):
        navigation["map"] = [
            edge
            for edge in navigation["map"]
            if isinstance(edge, dict) and _edge_ok(edge, included)
        ]
    if isinstance(navigation, dict) and isinstance(navigation.get("tabs"), list):
        navigation["tabs"] = [
            tab
            for tab in navigation["tabs"]
            if isinstance(tab, dict) and str(tab.get("screen_id")) in included
        ]

    for req in spec.get("requirements", []):
        if isinstance(req, dict) and isinstance(req.get("screens"), list):
            req["screens"] = [sid for sid in req["screens"] if str(sid) in included]

    if isinstance(spec.get("provenance"), dict):
        spec["provenance"]["screen_count"] = len(kept)
    if "screen_count" in spec:
        spec["screen_count"] = len(kept)

    validated = spec_contract.validate_spec(spec)
    paths.app_spec_json.write_text(json.dumps(validated, indent=2, ensure_ascii=False))
    log.info("feasibility.apply_scope.done", kept=len(kept), dropped=len(screens) - len(kept))


def scope_key(job_id: str) -> str:
    """Job-scoped MinIO key for the scope artifact (``jobs/{id}/scope/scope.json``)."""
    return build_key(job_id=job_id, kind=SCOPE_KIND, name=SCOPE_NAME)


def save_scope(storage: ArtifactStorage, job_id: str, scope: ScopeDecision) -> ArtifactRef:
    """Write ``scope`` as a new version of the job's ``scope.json`` and return its ref."""
    return storage.put(
        scope_key(job_id),
        scope.model_dump_json(indent=2).encode("utf-8"),
        content_type="application/json",
    )


def load_scope(
    storage: ArtifactStorage, job_id: str, *, version_id: str | None = None
) -> ScopeDecision:
    """Read the job's ``scope.json`` (latest, or a specific ``version_id``)."""
    raw = storage.get(scope_key(job_id), version_id=version_id)
    return ScopeDecision.model_validate_json(raw)
