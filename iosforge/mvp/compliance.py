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

JUDGE_PROMPT = """\
You are judging how faithfully a generated Flutter app reproduces an original
Android app, screen by screen, from screenshots.

In this directory:
- `screens/<id>.png` — the ORIGINAL (target) screenshots.
- `generated_screens/<id>.png` — the GENERATED app's screenshots, rendered by
  deep link `iosforge://screen/<id>`. A missing file means that screen failed to
  render.

For every id listed below, compare `screens/<id>.png` against
`generated_screens/<id>.png` (LOOK at both) and rate the visual fidelity.

Write a single file `judge.json` with EXACTLY this schema:

{
  "screens": [
    { "id": str, "score": float (0.0-1.0), "diffs": [str] }
  ],
  "flows_score": float (0.0-1.0)
}

- `score` 1.0 = pixel-faithful layout/content; 0.0 = absent or unrecognisable.
- `diffs` = concrete, actionable differences (missing widgets, wrong colors,
  wrong text, wrong layout) a developer can fix.
- `flows_score` = holistic judgement of whether navigation/flows are reproduced.

Ids to judge:
{ids}

Output ONLY the file `judge.json`.
"""


@dataclass(frozen=True)
class ComplianceWeights:
    """Weights for the compliance score (sum to 1.0 by convention)."""

    visual: float
    coverage: float
    flows: float

    @classmethod
    def from_settings(cls, settings: Settings) -> ComplianceWeights:
        return cls(
            visual=settings.compliance_weight_visual,
            coverage=settings.compliance_weight_coverage,
            flows=settings.compliance_weight_flows,
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
    scores: list[float] = []
    matched = 0
    for oid, gen in matches:
        if gen is None:
            score = 0.0
            diffs = ["screen not rendered in generated app"]
        else:
            matched += 1
            entry = judge_by_id.get(oid, {})
            score = float(entry.get("score", 0.0))
            raw_diffs = entry.get("diffs", [])
            diffs = [str(d) for d in raw_diffs] if isinstance(raw_diffs, list) else []
        scores.append(score)
        screens.append({"id": oid, "generated": gen, "score": score, "diffs": diffs})

    visual = sum(scores) / len(scores) if scores else 0.0
    coverage = matched / len(matches) if matches else 0.0
    flows = float(judge.get("flows_score", 0.0))
    compliance_score = weights.visual * visual + weights.coverage * coverage + weights.flows * flows
    if compliance_score >= threshold:
        status = "pass"
    elif compliance_score >= soft_floor:
        status = "soft_pass"
    else:
        status = "below_floor"

    return {
        "iteration": iteration,
        "compliance_score": compliance_score,
        "visual": visual,
        "coverage": coverage,
        "flows": flows,
        "status": status,
        "threshold": threshold,
        "soft_floor": soft_floor,
        "weights": {
            "visual": weights.visual,
            "coverage": weights.coverage,
            "flows": weights.flows,
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
        headline = diffs[0] if diffs else "visual mismatch"
        tasks.append(
            {
                "id": f"fix-{iteration}-{sid}",
                "type": "fix",
                "title": f"Fix screen {sid}: {headline}",
                "screens": [sid],
                "diffs": diffs,
                "deps": [],
            }
        )
    return {"tasks": tasks}


def _corrective_prompt(task: dict[str, Any]) -> str:
    sid = str(task.get("screens", ["?"])[0]) if task.get("screens") else "?"
    diffs = task.get("diffs", [])
    diff_lines = "\n".join(f"- {d}" for d in diffs) or "- (close the visual gap)"
    return f"""\
You are correcting one screen of the Flutter app under `flutter_app/` so it more
faithfully matches the original Android app.

Screen id: {sid}

Compare these two screenshots (LOOK at both):
- `screens/{sid}.png` — the ORIGINAL (target).
- `generated_screens/{sid}.png` — the CURRENT generated render.

Differences to fix:
{diff_lines}

EDIT the relevant files under `flutter_app/lib/` in place to close these
differences. Keep the deep-link routing (`iosforge://screen/<id>`) and the rest
of the app intact and compiling. Output ONLY changes under `flutter_app/`.
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
        claude_gen.run_task(paths, _corrective_prompt(task), timeout=timeout, tlog=tlog)

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
        visual = sum(scores) / len(scores)
        report["compliance_score"] = (
            weights.visual * visual
            + weights.coverage * float(report.get("coverage", 0.0))
            + weights.flows * float(report.get("flows", 0.0))
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
) -> dict[str, Any]:
    """Platform-agnostic refine loop: ``prepare`` builds + renders one iteration.

    The gate metric is the overall compliance score, or — when ``per_screen`` — the
    weakest individual screen, so the loop keeps correcting until EVERY screen meets
    the threshold. When ``freeze_passed``, a screen that reaches the threshold is
    locked (never re-corrected or regressed by judge/render noise). Stops on
    gate>=threshold, iteration>=max_iterations, or improvement<=0.01.
    """
    bound = log.bind(stage="compliance", run_dir=str(paths.run_dir))
    history: list[float] = []
    frozen: dict[str, float] = {}
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
        gate = _min_screen_score(report) if per_screen else float(report["compliance_score"])
        if gate >= threshold:
            report["stop_reason"] = "all_screens_met" if per_screen else "threshold_met"
            break
        if iteration >= max_iterations:
            report["stop_reason"] = "max_iterations"
            break
        if history and (gate - history[-1]) <= 0.01:
            report["stop_reason"] = "no_improvement"
            break
        history.append(gate)
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
