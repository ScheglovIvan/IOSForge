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

import json
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from iosforge.common.config import Settings
from iosforge.common.logging import get_logger
from iosforge.mvp import claude_gen, emulator
from iosforge.mvp.paths import RunPaths

log = get_logger("mvp.compliance")

FLUTTER_BIN = "/opt/flutter/bin/flutter"

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
    """Loop build -> render -> evaluate -> correct until compliant or exhausted.

    Stops on score>=threshold, iteration>=max_iterations, or improvement<=0.01 vs
    the previous iteration. Always returns the final selftest report dict; a
    ``below_floor`` status is reported but never raises (delivery is not blocked).
    """
    bound = log.bind(stage="compliance", run_dir=str(paths.run_dir))
    history: list[float] = []
    report: dict[str, Any] = {}
    for iteration in range(1, max_iterations + 1):
        build_apk(paths, timeout=timeout)
        render_generated(paths, avd=avd, timeout=timeout)
        report = evaluate(
            paths,
            iteration,
            weights=weights,
            threshold=threshold,
            soft_floor=soft_floor,
            history=history,
            timeout=timeout,
        )
        score = float(report["compliance_score"])
        if score >= threshold:
            report["stop_reason"] = "threshold_met"
            break
        if iteration >= max_iterations:
            report["stop_reason"] = "max_iterations"
            break
        if history and (score - history[-1]) <= 0.01:
            report["stop_reason"] = "no_improvement"
            break
        history.append(score)
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
