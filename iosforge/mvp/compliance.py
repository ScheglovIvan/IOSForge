"""Stage E — the Vision Judge: refine the generated SwiftUI app toward the original.

Pipeline: ``build_ios`` (xcodebuild for the Simulator) -> ``render_generated_ios``
(every screen launched with ``-screen-id <id>``, stable frames, the job's locale) ->
``match_screens`` (pure, by id) -> ``evaluate`` (vision-judge ``claude -p`` +
deterministic ``aggregate``) -> ``diffs_to_tasks`` (pure) -> ``apply_corrective``
(sandboxed SwiftUI screen tasks), driven by ``refine_ios_until_complete`` on top of
the unchanged ``_refine`` loop; ``nav_audit_ios`` is the structural audit.

``status == "below_floor"`` does NOT block delivery: the Job stays DONE and the
artifact is delivered; only the report content differs (the structural hard gate is
separate and parks the job in NEEDS_INPUT).

The vision-judge prompt is inlined here — the same deferred SPEC §6
(PromptSetProvider) tech-debt as ``analyze``.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from iosforge.common.config import Settings
from iosforge.common.logging import get_logger
from iosforge.mvp import claude_gen, simulator, swiftui_gen, xcode
from iosforge.mvp.paths import RunPaths
from iosforge.mvp.swiftui_ads import is_ad_component
from iosforge.mvp.swiftui_permissions import blank_comments_and_strings
from iosforge.mvp.swiftui_scaffold import build_plan

log = get_logger("mvp.compliance")


JUDGE_PROMPT = """\
You are judging how faithfully a generated native iOS (SwiftUI) app reproduces an
original iOS app, screen by screen, from screenshots.

In this directory:
- `screens/<id>.png` — the ORIGINAL (target) screenshots.
- `generated_screens/<id>.png` — the GENERATED app's screenshots, rendered on the
  iOS Simulator by launching the app with `-screen-id <id>`. A missing file means
  that screen failed to render.
- `removed_ads.json` (when present) — per screen id, the advertising the ORIGINAL
  showed and the clone removes ON PURPOSE (ad events captured on the device, ad
  components removed from the spec, analysis notes).

The clone ships WITHOUT advertising. Ad banners, native ad cards, app-open /
interstitial / rewarded overlays, "Loading ads" states and "may contain ads"
disclaimers on an ORIGINAL screenshot are NOT expected in the clone: never lower
`structure_score` for their absence and never list them in `diffs` — judge the
structure as if the ad were not on the original (the content around it reflows).

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
  wrong order/hierarchy) a developer can fix — NOT colour/font differences. Only
  list things that NEED a change: no notes about acceptable or expected
  differences (e.g. space freed by a removed ad), no praise, no "no fix needed".
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


def _original_screen_ids(paths: RunPaths) -> list[str]:
    data = json.loads(paths.screens_json.read_text())
    screens = data.get("screens", []) if isinstance(data, dict) else []
    return [str(s["id"]) for s in screens if isinstance(s, dict) and "id" in s]


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


_SENTENCE = re.compile(r"(?<=[.;!?])\s+")
_AD_WORD = re.compile(r"\b(?:ads?|advert\w*|interstitial)\b", re.I)
_PROJECT_NAME = re.compile(r"^name:\s*(\S+)", re.M)
_PROJECT_BUNDLE = re.compile(r"PRODUCT_BUNDLE_IDENTIFIER:\s*(\S+)")
REMOVED_ADS_JSON = "removed_ads.json"


def removed_ads(paths: RunPaths) -> dict[str, list[str]]:
    """Per original screen, the advertising the clone removes on purpose (facts, not vision).

    Sources: Frida ``ads_raw.json`` ad events (provider + format per screen), app_spec
    components that are ads, and app_spec ``layout_notes`` sentences about removed ads.
    """
    facts: dict[str, list[str]] = {}

    def add(screen: object, fact: str) -> None:
        sid = str(screen)
        if sid and sid != "None" and fact not in facts.setdefault(sid, []):
            facts[sid].append(fact)

    if paths.ads_raw_json.is_file():
        raw = json.loads(paths.ads_raw_json.read_text(encoding="utf-8"))
        for event in raw.get("ad_events", []) if isinstance(raw, dict) else []:
            if isinstance(event, dict):
                provider = event.get("provider", "ad network")
                add(event.get("screen"), f"captured ad: {provider} {event.get('format', 'ad')}")
    if paths.app_spec_json.is_file():
        spec = json.loads(paths.app_spec_json.read_text(encoding="utf-8"))
        for screen in spec.get("screens", []) if isinstance(spec, dict) else []:
            if not isinstance(screen, dict):
                continue
            for component in screen.get("components", []) or []:
                if isinstance(component, dict) and is_ad_component(component):
                    add(
                        screen.get("id"),
                        f"ad component: {component.get('type')} / {component.get('role')} "
                        f"{str(component.get('data') or '')[:80]}".strip(),
                    )
            for note in _SENTENCE.split(str(screen.get("layout_notes") or "")):
                if _AD_WORD.search(note):
                    add(screen.get("id"), f"analysis note: {note.strip()}")
    return facts


def _project_value(paths: RunPaths, pattern: re.Pattern[str], what: str) -> str:
    project = paths.xcode_app / "project.yml"
    match = pattern.search(project.read_text(encoding="utf-8")) if project.is_file() else None
    if match is None:
        raise RuntimeError(f"no {what} in {project}")
    return match.group(1)


def build_ios(paths: RunPaths, *, timeout: int = 1800) -> Path:
    """XcodeGen + xcodebuild the generated ``xcode_app`` for the iOS Simulator; return the .app.

    Raises :class:`~iosforge.mvp.simulator.SimulatorUnavailable` off-Mac and
    ``RuntimeError`` with the build errors when the project does not compile.
    """
    simulator.require_toolchain()
    scheme = _project_value(paths, _PROJECT_NAME, "target name")
    derived = paths.run_dir / "DerivedData"
    xcode.generate_project(paths.xcode_app)
    outcome = xcode.build(paths.xcode_app, scheme, derived_data=derived, timeout=timeout)
    (paths.run_dir / "xcodebuild.log").write_text(outcome.log, encoding="utf-8")
    if not outcome.ok:
        raise RuntimeError(f"xcodebuild failed: {outcome.errors[:20]}")
    return xcode.built_app(derived, scheme)


def render_generated_ios(
    paths: RunPaths,
    env: simulator.SimEnvironment,
    *,
    app: Path,
    screen_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Install ``app`` and render each original screen on the iOS Simulator.

    Pins the environment once, relaunches per screen with ``-screen-id`` and the job
    locale, keeps only stable frames (:func:`~iosforge.mvp.simulator.capture_stable`)
    and writes the same ``generated_screens.json`` the web renderer produced (plus
    per-screen ``frames`` / ``frame_diff`` evidence), so matching, judging and scoring
    are unchanged. Also writes ``removed_ads.json`` into the judge workspace.
    """
    bound = log.bind(stage="compliance.render_ios", run_dir=str(paths.run_dir))
    simulator.require_toolchain()
    bundle_id = _project_value(paths, _PROJECT_BUNDLE, "bundle id")
    simulator.pin_environment(env)
    xcode.install(env.udid, app)
    generated: list[dict[str, Any]] = []
    for sid in screen_ids if screen_ids is not None else _original_screen_ids(paths):
        out = paths.generated_screens_dir / f"{sid}.png"
        try:
            shot = simulator.render_screen(env, bundle_id, sid, out)
        except simulator.UnstableFrame as exc:
            bound.warning("compliance.render_ios.unstable", screen=sid, error=str(exc))
            generated.append(
                {"id": sid, "screenshot": f"generated_screens/{sid}.png", "unstable": True}
            )
            continue
        generated.append(
            {
                "id": sid,
                "screenshot": f"generated_screens/{sid}.png",
                "frames": shot.frames,
                "frame_diff": round(shot.diff, 5),
                "blank": shot.blank,
            }
        )
    result = {
        "package": "ios",
        "locale": env.locale,
        "appearance": env.appearance,
        "screens": generated,
    }
    paths.generated_screens_json.write_text(json.dumps(result, indent=2, ensure_ascii=False))
    paths.claude_ws.mkdir(parents=True, exist_ok=True)
    (paths.claude_ws / REMOVED_ADS_JSON).write_text(
        json.dumps(removed_ads(paths), indent=2, ensure_ascii=False), encoding="utf-8"
    )
    bound.info("compliance.render_ios.done", screens=len(generated), locale=env.locale)
    return result


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


def apply_corrective(
    paths: RunPaths, corrective_tasks: dict[str, Any], *, timeout: int = 1800
) -> Path:
    """Run the judge's fix tasks as sandboxed SwiftUI screen tasks (+ compile gate).

    :func:`iosforge.mvp.swiftui_gen.correct` reruns each affected screen with the
    original and generated screenshots plus the diffs; contract files stay restored.
    """
    tasks = corrective_tasks.get("tasks", [])
    if not tasks:
        log.info("compliance.apply_corrective.noop", run_dir=str(paths.run_dir))
        return paths.xcode_app
    swiftui_gen.correct(paths, list(tasks), timeout=timeout)
    return paths.xcode_app


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


def _structural_open_count(structural: dict[str, Any]) -> int:
    keys = ("missing_screens", "blank_screens", "dead_links", "missing_edges")
    return sum(len(structural.get(key, [])) for key in keys)


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


def _feature_sources(paths: RunPaths, screen_id: str) -> str:
    folder = paths.xcode_app / "App" / "Features" / screen_id
    return "\n".join(
        blank_comments_and_strings(f.read_text(encoding="utf-8", errors="replace"))
        for f in sorted(folder.rglob("*.swift"))
    )


def nav_audit_ios(paths: RunPaths) -> dict[str, Any]:
    """Static navigation audit of the generated SwiftUI app (same report shape as nav_audit).

    Every ``navigation.map`` / ``navigates_to`` edge between built screens must be wired in
    the source screen: ``router.show(.<to>)``, or the tab bar (both ends tab roots / screens
    showing the bar), or ``router.dismiss()`` back to a screen that leads here, or
    ``router.finishOnboarding()`` from onboarding to home. Dead links cannot exist (they do
    not compile); ``missing_screens`` are planned screens without a generated render.
    """
    spec = json.loads(paths.app_spec_json.read_text(encoding="utf-8"))
    plan = build_plan(spec)
    by_id = {e.screen_id: e for e in plan.entries}
    edges: dict[tuple[str, str], str | None] = {}
    for edge in (spec.get("navigation") or {}).get("map", []) or []:
        if isinstance(edge, dict):
            edges.setdefault((str(edge.get("from")), str(edge.get("to"))), edge.get("via"))
    for screen in spec.get("screens", []):
        if isinstance(screen, dict):
            for target in screen.get("navigates_to", []) or []:
                edges.setdefault((str(screen.get("id")), str(target)), None)
    home = plan.tabs[0].root.screen_id
    missing_edges: list[dict[str, Any]] = []
    sources: dict[str, str] = {}
    for (frm, to), via in edges.items():
        if frm not in by_id or to not in by_id or frm == to:
            continue
        src, dst = by_id[frm], by_id[to]
        code = sources.setdefault(frm, _feature_sources(paths, frm))
        shown = re.search(rf"\.show\(\s*\.{re.escape(dst.case_name)}\s*\)", code)
        via_tab = (
            plan.shows_tab_bar
            and dst.presentation == "tabRoot"
            and (src.presentation == "tabRoot" or src.shows_tab_bar)
        )
        back = "router.dismiss()" in code and (to, frm) in edges
        onboarding_chain = [
            e
            for e in plan.entries
            if e.presentation == "onboarding"
            and re.search(rf"\.show\(\s*\.{re.escape(e.case_name)}\s*\)", code)
        ]
        finishes = "finishOnboarding()" in code or any(
            "finishOnboarding()"
            in sources.setdefault(e.screen_id, _feature_sources(paths, e.screen_id))
            for e in onboarding_chain
        )
        closes_to_home = to == home and (
            (src.presentation in ("sheet", "fullScreenCover") and "router.dismiss()" in code)
            or (src.presentation == "onboarding" and finishes)
        )
        if not (shown or via_tab or back or closes_to_home):
            missing_edges.append({"from": frm, "to": to, "via_element": via})
    generated = (
        {
            str(g["id"])
            for g in json.loads(paths.generated_screens_json.read_text()).get("screens", [])
        }
        if paths.generated_screens_json.exists()
        else set()
    )
    missing_screens = [
        {"id": e.screen_id, "expected_route": f"-screen-id {e.screen_id}"}
        for e in plan.entries
        if e.screen_id not in generated
    ]
    return {
        "missing_screens": missing_screens,
        "dead_links": [],
        "missing_edges": missing_edges,
        "ok": not missing_edges and not missing_screens,
    }


def verify_ios(
    paths: RunPaths,
    env: simulator.SimEnvironment,
    *,
    weights: ComplianceWeights,
    threshold: float,
    soft_floor: float,
) -> dict[str, Any]:
    """One-pass iOS check: build, render every screen on the Simulator, vision-judge."""
    app = build_ios(paths)
    render_generated_ios(paths, env, app=app)
    return evaluate(
        paths, 0, weights=weights, threshold=threshold, soft_floor=soft_floor, history=[]
    )


def refine_ios_until_complete(
    paths: RunPaths,
    env: simulator.SimEnvironment,
    *,
    threshold: float,
    soft_floor: float,
    max_iterations: int,
    weights: ComplianceWeights,
    blank_max_bytes: int,
    timeout: int = 1800,
    structural_gate: bool = True,
) -> dict[str, Any]:
    """The Vision-Judge loop: Simulator render + Swift navigation audit.

    Each iteration builds the SwiftUI app, renders every screen on the iOS Simulator and
    vision-judges it; the structural audit is :func:`nav_audit_ios` plus blank screens
    (PNG size, and frames ``render_generated_ios`` flagged as never drawn). Corrections run
    through :func:`apply_corrective`, which routes SwiftUI apps to the sandboxed screen
    tasks of :func:`iosforge.mvp.swiftui_gen.correct`. Scoring, weights and the loop itself
    are the unchanged :func:`_refine`.
    """

    def _prepare() -> None:
        app = build_ios(paths, timeout=timeout)
        render_generated_ios(paths, env, app=app)

    def _audit() -> dict[str, Any]:
        structural = nav_audit_ios(paths)
        flagged = {b["id"]: b for b in blank_screens(paths, max_bytes=blank_max_bytes)}
        rendered = json.loads(paths.generated_screens_json.read_text()).get("screens", [])
        for screen in rendered:
            reason = (
                "never_drawn"
                if screen.get("blank")
                else "unstable"
                if screen.get("unstable")
                else ""
            )
            if reason and str(screen["id"]) not in flagged:
                flagged[str(screen["id"])] = {"id": str(screen["id"]), "reason": reason}
        structural["blank_screens"] = list(flagged.values())
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
