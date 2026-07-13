"""Stage E — compliance/refinement loop driving the generated app toward >=95%.

Pipeline (per .claude/state/DECISIONS.md, with the two user overrides applied):
``build_apk`` -> ``render_generated`` (deep-link driven, NOT crawl-based) ->
``match_screens`` (pure, by id) -> ``evaluate`` (vision-judge ``claude -p`` +
deterministic ``aggregate``) -> ``diffs_to_tasks`` (pure) -> ``apply_corrective``
(claude task runner), wrapped by the ``refine_until_compliant`` driver.

User overrides vs the recorded spec:
- The generated app is navigated by deep link ``iosforge://screen/<id>`` rather
  than re-crawled with the uiautomator crawler; screens are matched by id.
- ``status == "below_floor"`` does NOT block delivery: the Job stays DONE and the
  artifact is delivered; only the report content differs.

The vision-judge and corrective prompts are inlined here — the same deferred
SPEC §6 (PromptSetProvider) tech-debt as ``analyze``/``claude_gen``.
"""

from __future__ import annotations

import functools
import http.server
import json
import re
import shutil
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from iosforge.common.config import Settings
from iosforge.common.logging import get_logger
from iosforge.mvp import claude_gen, emulator
from iosforge.mvp.paths import RunPaths

log = get_logger("mvp.compliance")

FLUTTER_BIN = "/opt/flutter/bin/flutter"
_CHROMIUM_CANDIDATES = ("chromium", "chromium-browser", "google-chrome", "google-chrome-stable")

_GOROUTE_HEAD_RE = re.compile(r"GoRoute\(")
_PATH_ATTR_RE = re.compile(r"""path:\s*['"]([^'"]+)['"]""")
_NAME_ATTR_RE = re.compile(r"""name:\s*['"]([^'"]+)['"]""")
_NAV_CALL_RES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"""context\.go\(['"]([^'"]+)"""), "go"),
    (re.compile(r"""context\.push\(['"]([^'"]+)"""), "push"),
    (re.compile(r"""context\.(?:goNamed|pushNamed)\(['"]([^'"]+)"""), "named"),
    (re.compile(r"""GoRouter\.of\(context\)\.go\(['"]([^'"]+)"""), "go"),
    (re.compile(r"""GoRouter\.of\(context\)\.push\(['"]([^'"]+)"""), "push"),
)
_GOROUTE_WINDOW = 240

JUDGE_PROMPT = """\
You are judging how faithfully a generated Flutter app reproduces an original
Android app, screen by screen, from screenshots.

In this directory:
- `screens/<id>.png` — the ORIGINAL (target) screenshots.
- `generated_screens/<id>.png` — the GENERATED app's screenshots, rendered by
  deep link `iosforge://screen/<id>`. A missing file means that screen failed to
  render.

This clone DELIBERATELY uses a different visual design (new palette, gradients,
fonts, button styles) while keeping the SAME structure. So do NOT reward visual
similarity — reward STRUCTURAL fidelity and penalise looking like a clone.

For every id listed below, compare `screens/<id>.png` against
`generated_screens/<id>.png` (LOOK at both) and rate two independent things.

Write a single file `judge.json` with EXACTLY this schema:

{
  "screens": [
    { "id": str, "structure_score": float (0.0-1.0),
      "divergence_score": float (0.0-1.0), "diffs": [str] }
  ],
  "flows_score": float (0.0-1.0)
}

- `structure_score` 1.0 = SAME blocks in the SAME positions/order/hierarchy
  (app bars, lists, cards, buttons, sections), IGNORING colour/font/styling;
  0.0 = absent or a completely different layout.
- `divergence_score` 1.0 = CLEARLY different from the original across style
  (palette, gradients, fonts, button shapes), ICONOGRAPHY (different icon style,
  no reused brand marks/logo) and COPY (wording paraphrased, not verbatim); 0.0 =
  looks like a copy.
- `diffs` = concrete, actionable LAYOUT differences (missing/misplaced blocks,
  wrong order/hierarchy) a developer can fix — NOT colour/font differences.
- `flows_score` = holistic judgement of whether navigation/flows are reproduced.

Ids to judge:
{ids}

Output ONLY the file `judge.json`.
"""


@dataclass(frozen=True)
class ComplianceWeights:
    """Weights for the compliance score (structure/coverage/flows/divergence sum to 1.0).

    ``divergence_min`` is not a weight but a hard floor carried alongside the
    weights so it reaches :func:`aggregate` without threading a new parameter
    through every driver: a screen whose divergence is below it is a clone risk.
    """

    structure: float
    coverage: float
    flows: float
    divergence: float
    divergence_min: float = 0.0

    @classmethod
    def from_settings(cls, settings: Settings) -> ComplianceWeights:
        return cls(
            structure=settings.compliance_weight_structure,
            coverage=settings.compliance_weight_coverage,
            flows=settings.compliance_weight_flows,
            divergence=settings.compliance_weight_divergence,
            divergence_min=settings.compliance_divergence_min,
        )


def build_apk(paths: RunPaths, *, timeout: int = 1800) -> Path:
    """Build a debug APK from paths.flutter_app and copy it to paths.apk."""
    bound = log.bind(stage="compliance.build_apk", run_dir=str(paths.run_dir))
    res = subprocess.run(
        [FLUTTER_BIN, "build", "apk", "--debug"],
        cwd=paths.flutter_app,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if res.returncode != 0:
        bound.error("compliance.build_apk.failed", code=res.returncode, stderr=res.stderr[-2000:])
        raise RuntimeError(f"flutter build apk exited {res.returncode}: {res.stderr[-500:]}")
    built = paths.flutter_app / "build" / "app" / "outputs" / "flutter-apk" / "app-debug.apk"
    if not built.exists():
        raise RuntimeError(f"flutter build apk did not produce {built}")
    shutil.copy2(built, paths.apk)
    bound.info("compliance.build_apk.done", apk=str(paths.apk))
    return paths.apk


def _original_screen_ids(paths: RunPaths) -> list[str]:
    data = json.loads(paths.screens_json.read_text())
    screens = data.get("screens", []) if isinstance(data, dict) else []
    return [str(s["id"]) for s in screens if isinstance(s, dict) and "id" in s]


def render_generated(paths: RunPaths, *, avd: str, timeout: int = 1800) -> dict[str, Any]:
    """Install the generated APK and deep-link to each original screen id.

    Drives navigation deterministically with ``am start ... iosforge://screen/<id>``
    instead of the uiautomator crawler; screenshots land in
    paths.generated_screens_dir keyed by original id.
    """
    bound = log.bind(stage="compliance.render", run_dir=str(paths.run_dir), avd=avd)
    package = emulator.install_apk(paths.apk)
    generated: list[dict[str, str]] = []
    for sid in _original_screen_ids(paths):
        emulator.adb(
            "shell",
            "am",
            "start",
            "-W",
            "-a",
            "android.intent.action.VIEW",
            "-d",
            f"iosforge://screen/{sid}",
            package,
            timeout=timeout,
        )
        time.sleep(2)
        png = paths.generated_screens_dir / f"{sid}.png"
        png.write_bytes(emulator.adb_raw("exec-out", "screencap", "-p"))
        generated.append({"id": sid, "screenshot": f"generated_screens/{sid}.png"})
    result = {"package": package, "screens": generated}
    paths.generated_screens_json.write_text(json.dumps(result, indent=2, ensure_ascii=False))
    bound.info("compliance.render.done", screens=len(generated))
    return result


def _discover_chromium(preferred: str = "") -> str:
    """Locate a headless-capable Chromium/Chrome binary, or raise."""
    if preferred:
        return preferred
    for name in _CHROMIUM_CANDIDATES:
        found = shutil.which(name)
        if found:
            return found
    for path in ("/snap/bin/chromium", "/usr/bin/chromium-browser", "/usr/bin/google-chrome"):
        if Path(path).is_file():
            return path
    raise RuntimeError("no chromium/chrome binary found for web rendering")


def build_web(paths: RunPaths, *, timeout: int = 1800) -> Path:
    """Build ``paths.flutter_app`` for the web (release, self-contained assets).

    ``--no-web-resources-cdn`` bundles CanvasKit locally so headless Chromium under
    ``--no-sandbox`` renders without network. Success is checked by the presence of
    ``build/web/index.html`` (the root warning under a root user is not fatal).
    """
    bound = log.bind(stage="compliance.build_web", run_dir=str(paths.run_dir))
    app = paths.flutter_app
    if not (app / "web" / "index.html").is_file():
        subprocess.run(
            [FLUTTER_BIN, "create", "--platforms=web", "."],
            cwd=app,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    subprocess.run(
        [
            FLUTTER_BIN,
            "build",
            "web",
            "--release",
            "--no-web-resources-cdn",
            "--suppress-analytics",
        ],
        cwd=app,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    out = app / "build" / "web"
    if not (out / "index.html").is_file():
        raise RuntimeError("flutter build web did not produce build/web/index.html")
    bound.info("compliance.build_web.done", web=str(out))
    return out


def _snap_stage_dir(chromium_bin: str) -> Path | None:
    """A screenshot staging dir a *snap-confined* Chromium can actually write to.

    Snap Chromium runs under confinement and cannot write ``--screenshot`` to
    ``/tmp`` (it silently writes into its private namespace, so the file never
    appears at the real path). It CAN write under its own snap home. Return that
    dir for a snap binary, or ``None`` for a normal (unconfined) Chromium.
    """
    if "/snap/" not in chromium_bin:
        return None
    stage = Path.home() / "snap" / "chromium" / "common" / "iosforge_render"
    stage.mkdir(parents=True, exist_ok=True)
    return stage


def render_generated_web(
    paths: RunPaths,
    *,
    chromium_bin: str,
    wait_ms: int,
    window: str,
    timeout: int = 300,
) -> dict[str, Any]:
    """Serve ``build/web`` and screenshot each screen via ``/#/screen/<id>``.

    Uses the canonical preview route the codegen contract guarantees. ``--headless=old``
    writes the screenshot synchronously; software GL (SwiftShader) lets Flutter's
    CanvasKit paint without a GPU. Snap-confined Chromium can't write to ``/tmp``, so
    shots are staged in its snap home and moved into ``paths.generated_screens_dir``
    (keyed by original id, same shape as the emulator :func:`render_generated`). The
    HTTP server binds an ephemeral port and is always torn down.
    """
    bound = log.bind(stage="compliance.render_web", run_dir=str(paths.run_dir))
    web_dir = paths.flutter_app / "build" / "web"
    stage = _snap_stage_dir(chromium_bin)
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(web_dir))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    generated: list[dict[str, str]] = []
    try:
        for sid in _original_screen_ids(paths):
            png = paths.generated_screens_dir / f"{sid}.png"
            shot = (stage / f"{sid}.png") if stage is not None else png
            subprocess.run(
                [
                    chromium_bin,
                    "--headless=old",
                    "--no-sandbox",
                    "--hide-scrollbars",
                    "--enable-unsafe-swiftshader",
                    "--use-gl=angle",
                    "--use-angle=swiftshader-webgl",
                    f"--virtual-time-budget={wait_ms}",
                    f"--window-size={window}",
                    f"--screenshot={shot}",
                    f"http://127.0.0.1:{port}/#/screen/{sid}",
                ],
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
            if stage is not None and shot.is_file():
                shutil.move(str(shot), str(png))
            if png.is_file():
                generated.append({"id": sid, "screenshot": f"generated_screens/{sid}.png"})
    finally:
        server.shutdown()
        server.server_close()
    result = {"package": "web", "screens": generated}
    paths.generated_screens_json.write_text(json.dumps(result, indent=2, ensure_ascii=False))
    bound.info("compliance.render_web.done", screens=len(generated))
    return result


def match_screens(
    original: list[dict[str, Any]], generated: list[dict[str, Any]]
) -> list[tuple[str, str | None]]:
    """Pair original screens to generated ones by id (pure).

    Generated screens are keyed by original id. An original id with no generated
    render maps to ``None``; surplus generated screens are ignored.
    """
    gen_by_id = {
        str(g["id"]): str(g.get("screenshot") or "")
        for g in generated
        if isinstance(g, dict) and "id" in g
    }
    pairs: list[tuple[str, str | None]] = []
    for o in original:
        if not isinstance(o, dict) or "id" not in o:
            continue
        oid = str(o["id"])
        shot = gen_by_id.get(oid)
        pairs.append((oid, shot or None))
    return pairs


def aggregate(
    judge: dict[str, Any],
    matches: list[tuple[str, str | None]],
    *,
    weights: ComplianceWeights,
    threshold: float,
    soft_floor: float,
    iteration: int,
) -> dict[str, Any]:
    """Deterministically fold the vision-judge output into a compliance report (pure)."""
    judge_by_id: dict[str, dict[str, Any]] = {
        str(s["id"]): s for s in judge.get("screens", []) if isinstance(s, dict) and "id" in s
    }
    screens: list[dict[str, Any]] = []
    structure_scores: list[float] = []
    divergence_scores: list[float] = []
    matched = 0
    for oid, gen in matches:
        if gen is None:
            s_score = 0.0
            d_score = 0.0
            diffs = ["screen not rendered in generated app"]
        else:
            matched += 1
            entry = judge_by_id.get(oid, {})
            s_score = float(entry.get("structure_score", entry.get("score", 0.0)))
            d_score = float(entry.get("divergence_score", 0.0))
            raw_diffs = entry.get("diffs", [])
            diffs = [str(d) for d in raw_diffs] if isinstance(raw_diffs, list) else []
            divergence_scores.append(d_score)
        structure_scores.append(s_score)
        screens.append(
            {"id": oid, "generated": gen, "score": s_score, "divergence": d_score, "diffs": diffs}
        )

    structure = sum(structure_scores) / len(structure_scores) if structure_scores else 0.0
    divergence = sum(divergence_scores) / len(divergence_scores) if divergence_scores else 0.0
    coverage = matched / len(matches) if matches else 0.0
    flows = float(judge.get("flows_score", 0.0))
    compliance_score = (
        weights.structure * structure
        + weights.coverage * coverage
        + weights.flows * flows
        + weights.divergence * divergence
    )
    if matched > 0 and divergence < weights.divergence_min:
        status = "clone_risk"
    elif compliance_score >= threshold:
        status = "pass"
    elif compliance_score >= soft_floor:
        status = "soft_pass"
    else:
        status = "below_floor"

    return {
        "iteration": iteration,
        "compliance_score": compliance_score,
        "structure": structure,
        "divergence": divergence,
        "divergence_min": weights.divergence_min,
        "coverage": coverage,
        "flows": flows,
        "status": status,
        "threshold": threshold,
        "soft_floor": soft_floor,
        "weights": {
            "structure": weights.structure,
            "coverage": weights.coverage,
            "flows": weights.flows,
            "divergence": weights.divergence,
        },
        "screens": screens,
    }


def _prepare_judge_workspace(paths: RunPaths) -> None:
    paths.claude_ws.mkdir(parents=True, exist_ok=True)
    for name, src in (
        ("screens", paths.screens_dir),
        ("generated_screens", paths.generated_screens_dir),
    ):
        dest = paths.claude_ws / name
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(src, dest)


def evaluate(
    paths: RunPaths,
    iteration: int,
    *,
    weights: ComplianceWeights,
    threshold: float,
    soft_floor: float,
    history: list[float],
    timeout: int = 1800,
) -> dict[str, Any]:
    """Vision-judge the render then aggregate into paths.selftest_report_json."""
    bound = log.bind(stage="compliance.evaluate", run_dir=str(paths.run_dir), iteration=iteration)
    original = json.loads(paths.screens_json.read_text()).get("screens", [])
    generated = json.loads(paths.generated_screens_json.read_text()).get("screens", [])
    matches = match_screens(original, generated)

    _prepare_judge_workspace(paths)
    ids = "\n".join(f"- {oid}" for oid, _ in matches) or "- (none)"
    prompt = JUDGE_PROMPT.replace("{ids}", ids)
    (paths.claude_ws / "JUDGE.md").write_text(prompt)
    res = subprocess.run(
        [claude_gen.CLAUDE_BIN, "-p", prompt, "--permission-mode", "acceptEdits"],
        cwd=paths.claude_ws,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if res.returncode != 0:
        bound.warning(
            "compliance.evaluate.cli_failed", code=res.returncode, stderr=res.stderr[-500:]
        )
    judge_path = paths.claude_ws / "judge.json"
    if not judge_path.exists():
        raise RuntimeError("vision-judge did not produce judge.json")
    judge = json.loads(judge_path.read_text())

    report = aggregate(
        judge,
        matches,
        weights=weights,
        threshold=threshold,
        soft_floor=soft_floor,
        iteration=iteration,
    )
    report["history"] = [*history, report["compliance_score"]]
    paths.selftest_report_json.write_text(json.dumps(report, indent=2, ensure_ascii=False))
    bound.info(
        "compliance.evaluate.done",
        score=report["compliance_score"],
        status=report["status"],
    )
    return report


def diffs_to_tasks(report: dict[str, Any], iteration: int) -> dict[str, Any]:
    """Turn below-threshold / missing screens into corrective fix tasks (pure).

    Reuses the tasks.json schema with ``type == "fix"``; each task carries its
    ``diffs`` so the corrective runner can reference them in the prompt.
    """
    threshold = float(report.get("threshold", 0.95))
    tasks: list[dict[str, Any]] = []
    for screen in report.get("screens", []):
        sid = str(screen["id"])
        score = float(screen.get("score", 0.0))
        if score >= threshold:
            continue
        diffs = [str(d) for d in screen.get("diffs", [])]
        headline = diffs[0] if diffs else "layout mismatch"
        tasks.append(
            {
                "id": f"fix-{iteration}-{sid}",
                "type": "fix",
                "title": f"Fix screen {sid} layout: {headline}",
                "screens": [sid],
                "diffs": diffs,
                "deps": [],
            }
        )

    divergence_min = float(report.get("divergence_min", 0.0))
    if divergence_min > 0.0 and float(report.get("divergence", 1.0)) < divergence_min:
        for screen in report.get("screens", []):
            sid = str(screen["id"])
            if float(screen.get("divergence", 1.0)) >= divergence_min:
                continue
            tasks.append(
                {
                    "id": f"diverge-{iteration}-{sid}",
                    "type": "diverge",
                    "title": f"Restyle screen {sid} to look less like the original",
                    "screens": [sid],
                    "deps": [],
                }
            )

    structural = report.get("structural")
    if isinstance(structural, dict):
        for finding in structural.get("missing_screens", []):
            sid = str(finding.get("id"))
            route = str(finding.get("expected_route") or f"/{sid}")
            tasks.append(
                {
                    "id": f"add-{iteration}-{sid}",
                    "type": "add_screen",
                    "title": f"Add missing screen {sid} at {route}",
                    "screens": [sid],
                    "expected_route": route,
                    "deps": [],
                }
            )
        for finding in structural.get("blank_screens", []):
            sid = str(finding.get("id"))
            tasks.append(
                {
                    "id": f"blank-{iteration}-{sid}",
                    "type": "fix_blank",
                    "title": f"Implement blank screen {sid}",
                    "screens": [sid],
                    "deps": [],
                }
            )
        for idx, finding in enumerate(structural.get("dead_links", [])):
            target = str(finding.get("target"))
            file = str(finding.get("file"))
            kind = str(finding.get("kind"))
            tasks.append(
                {
                    "id": f"deadlink-{iteration}-{idx}",
                    "type": "fix_dead_link",
                    "title": f"Resolve dead link to {target}",
                    "target": target,
                    "file": file,
                    "kind": kind,
                    "deps": [],
                }
            )
        for idx, finding in enumerate(structural.get("missing_edges", [])):
            frm = str(finding.get("from"))
            to = str(finding.get("to"))
            via = finding.get("via_element")
            tasks.append(
                {
                    "id": f"edge-{iteration}-{idx}",
                    "type": "add_edge",
                    "title": f"Wire navigation {frm} -> {to}",
                    "from": frm,
                    "to": to,
                    "via_element": via,
                    "deps": [],
                }
            )
    return {"tasks": tasks}


def _add_screen_prompt(task: dict[str, Any]) -> str:
    sid = str(task.get("screens", ["?"])[0]) if task.get("screens") else "?"
    route = str(task.get("expected_route") or f"/{sid}")
    return f"""\
You are extending the Flutter app under `flutter_app/` to add a MISSING screen.

Screen id: {sid}
Reference screenshot: `screens/{sid}.png` (LOOK at it and reproduce its layout).

Do all of the following, editing files under `flutter_app/lib/` in place:
- Create the screen widget faithful to `screens/{sid}.png`.
- Register a concrete route `GoRoute(path: '{route}')` that renders it.
- Keep the canonical preview route `/#/screen/:id` reachable for every screen.
- Keep the whole app compiling. Output ONLY changes under `flutter_app/`.
"""


def _fix_blank_prompt(task: dict[str, Any]) -> str:
    sid = str(task.get("screens", ["?"])[0]) if task.get("screens") else "?"
    return f"""\
You are fixing a BLANK screen in the Flutter app under `flutter_app/`.

Screen id: {sid}
The preview route `/#/screen/{sid}` currently renders (near-)blank.

LOOK at `screens/{sid}.png` (the ORIGINAL target) and implement the screen UI so
`/#/screen/{sid}` renders that content. EDIT files under `flutter_app/lib/` in place;
keep the app compiling. Output ONLY changes under `flutter_app/`.
"""


def _fix_dead_link_prompt(task: dict[str, Any]) -> str:
    target = str(task.get("target", "?"))
    file = str(task.get("file", "?"))
    return f"""\
You are fixing a DEAD navigation link in the Flutter app under `flutter_app/`.

A navigation call to `{target}` in `{file}` resolves to NO registered route.

Either register a matching `GoRoute` for `{target}`, OR correct the call to point at
an existing route. EDIT files under `flutter_app/lib/` in place; keep the app
compiling. Output ONLY changes under `flutter_app/`.
"""


def _add_edge_prompt(task: dict[str, Any]) -> str:
    frm = str(task.get("from", "?"))
    to = str(task.get("to", "?"))
    via = task.get("via_element")
    via_text = f"the `{via}` element" if via else "the appropriate control"
    return f"""\
You are wiring a MISSING navigation edge in the Flutter app under `flutter_app/`.

From screen `{frm}`, {via_text} must navigate to screen `{to}`.

EDIT the source of screen `{frm}` under `flutter_app/lib/` so {via_text} calls
`context.go(...)` targeting the route of screen `{to}` (its registered `GoRoute`
path, e.g. `/{to}`). Keep the app compiling. Output ONLY changes under `flutter_app/`.
"""


def _diverge_prompt(task: dict[str, Any]) -> str:
    sid = str(task.get("screens", ["?"])[0]) if task.get("screens") else "?"
    return f"""\
Screen `{sid}` of the Flutter app under `flutter_app/` still LOOKS TOO MUCH like
the original app — it must be visually distinct to avoid a clone complaint.

Restyle ONLY the appearance of screen `{sid}` using the app's design system:
apply the divergent theme, `lib/ui/components/` widgets, the `design_tokens`
palette/gradients/fonts and button styles. Change colours, gradients, fonts,
button/card shapes and spacing so it reads as a different product.

Do NOT change the LAYOUT — keep the same blocks in the same positions, the same
navigation and the same content/text. EDIT files under `flutter_app/lib/` in
place; keep the app compiling. Output ONLY changes under `flutter_app/`.
"""


def _corrective_prompt(task: dict[str, Any]) -> str:
    kind = str(task.get("type", "fix"))
    if kind == "add_screen":
        return _add_screen_prompt(task)
    if kind == "fix_blank":
        return _fix_blank_prompt(task)
    if kind == "fix_dead_link":
        return _fix_dead_link_prompt(task)
    if kind == "add_edge":
        return _add_edge_prompt(task)
    if kind == "diverge":
        return _diverge_prompt(task)
    sid = str(task.get("screens", ["?"])[0]) if task.get("screens") else "?"
    diffs = task.get("diffs", [])
    diff_lines = "\n".join(f"- {d}" for d in diffs) or "- (close the layout gap)"
    return f"""\
You are correcting the LAYOUT of one screen of the Flutter app under `flutter_app/`
so its structure matches the original app (block placement/order/hierarchy).

Screen id: {sid}

Compare these two screenshots (LOOK at both) for STRUCTURE, not styling:
- `screens/{sid}.png` — the ORIGINAL (target) layout.
- `generated_screens/{sid}.png` — the CURRENT generated render.

Layout differences to fix:
{diff_lines}

EDIT the relevant files under `flutter_app/lib/` in place, composing from
`lib/ui/components/`. Fix placement/order/hierarchy only — keep the DIVERGENT
design (new palette/fonts/gradients); do NOT copy the original's colours or fonts.
Keep the deep-link routing (`iosforge://screen/<id>`) and the app compiling.
Output ONLY changes under `flutter_app/`.
"""


def _restore_accumulator(paths: RunPaths) -> None:
    paths.claude_ws.mkdir(parents=True, exist_ok=True)
    shutil.copy2(paths.screens_json, paths.claude_ws / "screens.json")
    if paths.app_spec_json.exists():
        shutil.copy2(paths.app_spec_json, paths.claude_ws / "app_spec.json")
    for name, src in (
        ("screens", paths.screens_dir),
        ("generated_screens", paths.generated_screens_dir),
    ):
        dest = paths.claude_ws / name
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(src, dest)
    ws_app = paths.claude_ws / "flutter_app"
    if ws_app.exists():
        shutil.rmtree(ws_app)
    shutil.copytree(paths.flutter_app, ws_app)


def apply_corrective(
    paths: RunPaths, corrective_tasks: dict[str, Any], *, timeout: int = 1800
) -> Path:
    """Run each fix task against the restored accumulator, then move it back.

    Reuses the Stage D task runner (:func:`claude_gen.run_task`); the corrective
    prompt references both the original and generated screenshots plus the diffs.
    """
    bound = log.bind(stage="compliance.apply_corrective", run_dir=str(paths.run_dir))
    tasks = corrective_tasks.get("tasks", [])
    if not tasks:
        bound.info("compliance.apply_corrective.noop")
        return paths.flutter_app

    _restore_accumulator(paths)
    ws_app = paths.claude_ws / "flutter_app"
    for task in tasks:
        tlog = bound.bind(task_id=str(task.get("id")))
        tlog.info("compliance.apply_corrective.task")
        claude_gen.run_task(paths.claude_ws, _corrective_prompt(task), timeout=timeout, tlog=tlog)

    pubspec = ws_app / "pubspec.yaml"
    main_dart = ws_app / "lib" / "main.dart"
    if not (pubspec.exists() and main_dart.exists()):
        raise RuntimeError(
            "corrective runner left an invalid flutter_app/ "
            f"(pubspec={pubspec.exists()}, main.dart={main_dart.exists()})"
        )
    if paths.flutter_app.exists():
        shutil.rmtree(paths.flutter_app)
    shutil.move(str(ws_app), str(paths.flutter_app))
    bound.info("compliance.apply_corrective.done", tasks=len(tasks))
    return paths.flutter_app


def _min_screen_score(report: dict[str, Any]) -> float:
    scores = [float(s.get("score", 0.0)) for s in report.get("screens", [])]
    return min(scores) if scores else 0.0


def _apply_freeze(
    report: dict[str, Any], frozen: dict[str, float], threshold: float, weights: ComplianceWeights
) -> None:
    """Lock screens that ever reached the threshold: hold their score and clear
    their diffs so they are neither re-corrected nor regressed by judge/render
    noise. Recomputes the overall score from the frozen-merged per-screen scores.
    """
    for screen in report.get("screens", []):
        sid = str(screen.get("id"))
        if sid in frozen:
            screen["score"] = frozen[sid]
            screen["diffs"] = []
            screen["frozen"] = True
        elif float(screen.get("score", 0.0)) >= threshold:
            frozen[sid] = float(screen["score"])
    scores = [float(s.get("score", 0.0)) for s in report.get("screens", [])]
    if scores:
        structure = sum(scores) / len(scores)
        report["structure"] = structure
        report["compliance_score"] = (
            weights.structure * structure
            + weights.coverage * float(report.get("coverage", 0.0))
            + weights.flows * float(report.get("flows", 0.0))
            + weights.divergence * float(report.get("divergence", 0.0))
        )


def _refine(
    paths: RunPaths,
    prepare: Callable[[], None],
    *,
    threshold: float,
    soft_floor: float,
    max_iterations: int,
    weights: ComplianceWeights,
    timeout: int,
    per_screen: bool = False,
    freeze_passed: bool = False,
    audit: Callable[[], dict[str, Any]] | None = None,
    structural_gate: bool = True,
) -> dict[str, Any]:
    """Platform-agnostic refine loop: ``prepare`` builds + renders one iteration.

    The gate metric is the overall compliance score, or — when ``per_screen`` — the
    weakest individual screen, so the loop keeps correcting until EVERY screen meets
    the threshold. When ``freeze_passed``, a screen that reaches the threshold is
    locked (never re-corrected or regressed by judge/render noise).

    When ``audit`` is given, a structural report is merged into ``report["structural"]``
    each iteration and the composite gate additionally requires ``structural["ok"]``
    (when ``structural_gate``). ``no_improvement`` then fires only if the visual gate
    stalled AND the number of open structural findings did not decrease — the loop
    keeps running while it is still closing structural gaps. Stops on the composite
    pass (``all_closed`` with an audit, else ``all_screens_met``/``threshold_met``),
    ``max_iterations``, or ``no_improvement``.
    """
    bound = log.bind(stage="compliance", run_dir=str(paths.run_dir))
    history: list[float] = []
    frozen: dict[str, float] = {}
    prev_open: int | None = None
    report: dict[str, Any] = {}
    for iteration in range(1, max_iterations + 1):
        prepare()
        report = evaluate(
            paths,
            iteration,
            weights=weights,
            threshold=threshold,
            soft_floor=soft_floor,
            history=history,
            timeout=timeout,
        )
        if freeze_passed:
            _apply_freeze(report, frozen, threshold, weights)
        structural: dict[str, Any] | None = None
        open_count = 0
        structural_ok = True
        if audit is not None:
            structural = audit()
            report["structural"] = structural
            open_count = _structural_open_count(structural)
            if structural_gate:
                structural_ok = bool(structural.get("ok"))
        gate = _min_screen_score(report) if per_screen else float(report["compliance_score"])
        divergence_ok = float(report.get("divergence", 0.0)) >= weights.divergence_min
        if gate >= threshold and structural_ok and divergence_ok:
            if audit is not None:
                report["stop_reason"] = "all_closed"
            else:
                report["stop_reason"] = "all_screens_met" if per_screen else "threshold_met"
            break
        if iteration >= max_iterations:
            report["stop_reason"] = "max_iterations"
            break
        visual_stalled = bool(history) and (gate - history[-1]) <= 0.01
        structural_closing = (
            structural is not None and prev_open is not None and open_count < prev_open
        )
        if visual_stalled and not structural_closing:
            report["stop_reason"] = "no_improvement"
            break
        history.append(gate)
        prev_open = open_count
        corrective = diffs_to_tasks(report, iteration)
        paths.corrective_tasks_json.write_text(json.dumps(corrective, indent=2, ensure_ascii=False))
        apply_corrective(paths, corrective, timeout=timeout)

    paths.selftest_report_json.write_text(json.dumps(report, indent=2, ensure_ascii=False))
    bound.info(
        "compliance.done",
        score=report.get("compliance_score"),
        status=report.get("status"),
        stop_reason=report.get("stop_reason"),
    )
    return report


def refine_until_compliant(
    paths: RunPaths,
    *,
    threshold: float,
    soft_floor: float,
    max_iterations: int,
    weights: ComplianceWeights,
    avd: str,
    timeout: int = 1800,
) -> dict[str, Any]:
    """Emulator refine loop: APK build -> deep-link render -> evaluate -> correct."""

    def _prepare() -> None:
        build_apk(paths, timeout=timeout)
        render_generated(paths, avd=avd, timeout=timeout)

    return _refine(
        paths,
        _prepare,
        threshold=threshold,
        soft_floor=soft_floor,
        max_iterations=max_iterations,
        weights=weights,
        timeout=timeout,
    )


def refine_until_compliant_web(
    paths: RunPaths,
    *,
    threshold: float,
    soft_floor: float,
    max_iterations: int,
    weights: ComplianceWeights,
    chromium_bin: str = "",
    wait_ms: int,
    window: str,
    timeout: int = 1800,
    require_all_screens: bool = False,
) -> dict[str, Any]:
    """Web refine loop: flutter build web -> headless Chromium render -> evaluate
    -> correct weak screens, iterating toward ``threshold``. When
    ``require_all_screens`` is set, the loop runs until EVERY screen meets the
    threshold (weakest-screen gate), not just the overall score."""
    chromium = _discover_chromium(chromium_bin)

    def _prepare() -> None:
        build_web(paths, timeout=timeout)
        render_generated_web(paths, chromium_bin=chromium, wait_ms=wait_ms, window=window)

    return _refine(
        paths,
        _prepare,
        threshold=threshold,
        soft_floor=soft_floor,
        max_iterations=max_iterations,
        weights=weights,
        timeout=timeout,
        per_screen=require_all_screens,
        freeze_passed=require_all_screens,
    )


def verify_web(
    paths: RunPaths,
    *,
    weights: ComplianceWeights,
    threshold: float,
    soft_floor: float,
    chromium_bin: str = "",
    wait_ms: int,
    window: str,
) -> dict[str, Any]:
    """Web screen-similarity check: build web, render each screen in headless
    Chromium, vision-judge vs the originals. One pass, no auto-refine."""
    chromium = _discover_chromium(chromium_bin)
    build_web(paths)
    render_generated_web(paths, chromium_bin=chromium, wait_ms=wait_ms, window=window)
    return evaluate(
        paths,
        0,
        weights=weights,
        threshold=threshold,
        soft_floor=soft_floor,
        history=[],
    )


def _dart_sources(paths: RunPaths) -> list[tuple[Path, str]]:
    lib = paths.flutter_app / "lib"
    sources: list[tuple[Path, str]] = []
    if not lib.is_dir():
        return sources
    for path in sorted(lib.rglob("*.dart")):
        try:
            sources.append((path, path.read_text(encoding="utf-8", errors="replace")))
        except OSError:
            continue
    return sources


def _goroute_windows(text: str) -> list[str]:
    return [text[m.start() : m.start() + _GOROUTE_WINDOW] for m in _GOROUTE_HEAD_RE.finditer(text)]


def _expected_route(screen: dict[str, Any]) -> str:
    sid = str(screen.get("id", ""))
    return str(screen.get("route") or f"/{sid}")


def _param_route_matches(pattern: str, target: str) -> bool:
    pat_parts = pattern.strip("/").split("/")
    tgt_parts = target.strip("/").split("/")
    if len(pat_parts) != len(tgt_parts):
        return False
    for pat, tgt in zip(pat_parts, tgt_parts, strict=True):
        if pat.startswith(":"):
            if not tgt:
                return False
            continue
        if pat != tgt:
            return False
    return True


def _structural_open_count(structural: dict[str, Any]) -> int:
    keys = ("missing_screens", "blank_screens", "dead_links", "missing_edges")
    return sum(len(structural.get(key, [])) for key in keys)


def nav_audit(paths: RunPaths) -> dict[str, Any]:
    """Static navigation audit of the generated ``flutter_app/lib/`` (no build/render).

    Regex-scans the Dart sources for registered ``GoRoute(path: ...)`` routes and
    ``context.go/push/goNamed`` / ``GoRouter.of(context).go/push`` navigation calls,
    then cross-references them against the original nav graph in ``screens.json``
    (``id``/``route``/``navigates_to[]={to, via_element}``). Parametric routes
    (containing ``:``, e.g. the ``/screen/:id`` preview route) are tracked separately
    and excluded from the concrete-route existence set, but still used to cover
    parametric nav targets (``/screen/123`` is covered by ``/screen/:id``).

    Findings:
    - ``missing_screens``: a spec screen whose expected route (its ``route`` field
      else ``/{id}``, mirroring ``constitution._routes``) is not registered.
    - ``dead_links``: a go/push target matching no concrete route and no parametric
      route, or a named target with no ``name -> path`` entry.
    - ``missing_edges``: an original ``navigates_to`` edge with no matching nav call
      originating from the source screen's file. A screen's file is located via its
      feature dir ``lib/features/<id>/`` or the file registering its route; the
      shared ``lib/core/router/`` is always included in the nav-call scan. Edges
      whose source file cannot be confidently located are SKIPPED (no false positive).

    Honest failure modes (per the locked design):
    - Dynamically-built route strings (string interpolation/concatenation) are not
      matched → false negatives (a real route may be reported missing/dead). Nav
      targets containing ``$`` (Dart interpolation) are skipped rather than reported
      as dead links, and a ``?query`` suffix is stripped before route matching.
    - Named-route indirection is best-effort: the ``name -> path`` map is parsed from
      a bounded window after each ``GoRoute(``, so unusual formatting can miss.
    - Shared-widget navigation is only mitigated by also scanning ``lib/core/router/``;
      nav wired through other shared widgets may be misattributed or skipped.
    """
    sources = _dart_sources(paths)

    concrete_routes: set[str] = set()
    parametric_routes: set[str] = set()
    name_map: dict[str, str] = {}
    route_files: dict[str, set[Path]] = {}
    nav_calls: list[tuple[str, Path, str]] = []

    for path, text in sources:
        for window in _goroute_windows(text):
            pm = _PATH_ATTR_RE.search(window)
            if pm is None:
                continue
            route = pm.group(1)
            if ":" in route:
                parametric_routes.add(route)
            else:
                concrete_routes.add(route)
            route_files.setdefault(route, set()).add(path)
            nm = _NAME_ATTR_RE.search(window)
            if nm is not None:
                name_map[nm.group(1)] = route
        for pattern, call_kind in _NAV_CALL_RES:
            for m in pattern.finditer(text):
                nav_calls.append((m.group(1), path, call_kind))

    data = json.loads(paths.screens_json.read_text())
    screens = data.get("screens", []) if isinstance(data, dict) else []
    screen_route: dict[str, str] = {}
    for screen in screens:
        if isinstance(screen, dict) and "id" in screen:
            screen_route[str(screen["id"])] = _expected_route(screen)

    def _route_present(route: str) -> bool:
        return (
            route in concrete_routes
            or route in parametric_routes
            or any(_param_route_matches(pattern, route) for pattern in parametric_routes)
        )

    missing_screens: list[dict[str, Any]] = []
    for sid, route in screen_route.items():
        if not _route_present(route):
            missing_screens.append({"id": sid, "expected_route": route})

    dead_links: list[dict[str, Any]] = []
    for target, path, call_kind in nav_calls:
        if "$" in target:
            continue
        clean = target.split("?", 1)[0]
        if call_kind == "named":
            resolved = clean in name_map
        else:
            resolved = clean in concrete_routes or any(
                _param_route_matches(pattern, clean) for pattern in parametric_routes
            )
        if not resolved:
            dead_links.append({"target": target, "file": path.name, "kind": call_kind})

    router_files = {
        path for path, _ in sources if "core/router" in path.as_posix().replace("\\", "/")
    }

    def _source_files(sid: str, route: str) -> set[Path] | None:
        feature_marker = f"/features/{sid}/"
        located: set[Path] = {
            path for path, _ in sources if feature_marker in path.as_posix().replace("\\", "/")
        }
        located |= route_files.get(route, set())
        if not located:
            return None
        return located | router_files

    missing_edges: list[dict[str, Any]] = []
    for screen in screens:
        if not isinstance(screen, dict) or "id" not in screen:
            continue
        sid = str(screen["id"])
        src_files = _source_files(sid, screen_route.get(sid, f"/{sid}"))
        if src_files is None:
            continue
        for edge in screen.get("navigates_to", []) or []:
            if not isinstance(edge, dict):
                continue
            to = str(edge.get("to", ""))
            if not to:
                continue
            via = edge.get("via_element")
            to_route = screen_route.get(to, f"/{to}")
            reproduced = False
            for target, path, call_kind in nav_calls:
                if path not in src_files:
                    continue
                if call_kind == "named":
                    if name_map.get(target) == to_route:
                        reproduced = True
                        break
                elif target == to_route:
                    reproduced = True
                    break
            if not reproduced:
                missing_edges.append({"from": sid, "to": to, "via_element": via})

    ok = not (missing_screens or dead_links or missing_edges)
    return {
        "ok": ok,
        "routes_registered": sorted(concrete_routes | parametric_routes),
        "missing_screens": missing_screens,
        "dead_links": dead_links,
        "missing_edges": missing_edges,
    }


def blank_screens(paths: RunPaths, *, max_bytes: int) -> list[dict[str, Any]]:
    """Flag generated screenshots that are likely blank, by PNG byte size (no Pillow).

    A near-uniform image compresses to a tiny zlib stream, so any generated
    ``generated_screens/<id>.png`` under ``max_bytes`` is flagged as
    ``{"id": <stem>, "bytes": <size>, "reason": "near_uniform"}``.

    Failure mode: a minimal-but-intentional screen (a solid splash) can be a
    false positive; a complex-but-wrong screen is not caught. Acceptable for the
    MVP with zero image dependencies.
    """
    flagged: list[dict[str, Any]] = []
    for png in sorted(paths.generated_screens_dir.glob("*.png")):
        size = png.stat().st_size
        if size < max_bytes:
            flagged.append({"id": png.stem, "bytes": size, "reason": "near_uniform"})
    return flagged


def refine_web_until_complete(
    paths: RunPaths,
    *,
    threshold: float,
    soft_floor: float,
    max_iterations: int,
    weights: ComplianceWeights,
    chromium_bin: str = "",
    wait_ms: int,
    window: str,
    blank_max_bytes: int,
    timeout: int = 1800,
    structural_gate: bool = True,
) -> dict[str, Any]:
    """Web refine loop with a structural gate (Stage VERIFY).

    Mirrors :func:`refine_until_compliant_web` (per-screen visual gate, freeze passed
    screens) but also runs :func:`nav_audit` + :func:`blank_screens` each iteration.
    The composite ``ok`` requires the three nav-audit lists AND ``blank_screens`` to be
    empty. When ``structural_gate`` is False the findings are still recorded (and drive
    corrective tasks) but do not block the composite gate.
    """
    chromium = _discover_chromium(chromium_bin)

    def _prepare() -> None:
        build_web(paths, timeout=timeout)
        render_generated_web(paths, chromium_bin=chromium, wait_ms=wait_ms, window=window)

    def _audit() -> dict[str, Any]:
        structural = nav_audit(paths)
        structural["blank_screens"] = blank_screens(paths, max_bytes=blank_max_bytes)
        structural["ok"] = bool(structural["ok"]) and not structural["blank_screens"]
        return structural

    return _refine(
        paths,
        _prepare,
        threshold=threshold,
        soft_floor=soft_floor,
        max_iterations=max_iterations,
        weights=weights,
        timeout=timeout,
        per_screen=True,
        freeze_passed=True,
        audit=_audit,
        structural_gate=structural_gate,
    )
