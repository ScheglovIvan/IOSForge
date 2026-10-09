"""SwiftUI codegen (Phase 3): deterministic scaffold, then theme → components → screens.

The scaffold (:mod:`iosforge.mvp.swiftui_scaffold`) hard-wires navigation, tabs,
headless mode and permission prompters. The local Claude Code CLI then runs a
deterministic task DAG (no LLM decomposition) through
:func:`iosforge.mvp.analyze.topo_layers`: theme → component library (incl. the
single ``AppTabBar``) → every screen in parallel, each in its own sandbox copy of
the workspace from which only the screen's owned paths are harvested. The compile
gate restores the contract, lints permissions and builds with xcodebuild, looping
a bounded fix task. Not yet wired into ``run_job`` (future ``codegen_target``).
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from iosforge.common.logging import get_logger
from iosforge.mvp import claude_gen, frida_ingest, xcode
from iosforge.mvp.analyze import stage_archive_context, topo_layers
from iosforge.mvp.feasibility import apply_scope
from iosforge.mvp.paths import RunPaths
from iosforge.mvp.scope_models import FeasibilityReport, ScopeCounts, ScopeDecision, ScreenScope
from iosforge.mvp.swiftui_permissions import permission_violations
from iosforge.mvp.swiftui_prompters import PROMPTERS
from iosforge.mvp.swiftui_prompts import (
    APP_DIR,
    compile_fix_prompt,
    components_prompt,
    screen_prompt,
    theme_prompt,
)
from iosforge.mvp.swiftui_scaffold import (
    AppIdentity,
    ContractReport,
    NavPlan,
    ScreenEntry,
    build_plan,
    declared_kinds,
    enforce_contract,
    pending_screens,
    target_name,
    write_scaffold,
)

log = get_logger("mvp.swiftui_gen")

REFERENCE_DIR = Path(__file__).resolve().parents[2] / "docs" / "swiftui-reference"
UNKNOWN_SCREEN_ID = "__unknown__"
TASK_THEME = "theme"
TASK_COMPONENTS = "components"
SHARED = "shared"

_FEATURE_PATH = re.compile(r"App/Features/([^/:]+)/")
_FIXTURES_PATH = re.compile(r"App/Fixtures/(Fixtures[^/:]*\.swift)")


@dataclass
class TaskRun:
    """One model task: id, wall time, CLI return code, writes discarded by the harvest."""

    task: str
    seconds: float
    returncode: int
    ignored: list[str] = field(default_factory=list)


@dataclass
class GateCheck:
    """One compile-gate pass: errors, xcodebuild log, contract enforcement report."""

    errors: list[str]
    log: str = ""
    contract: ContractReport = field(default_factory=ContractReport)


@dataclass
class FixRound:
    """Errors one fix round started from, attributed to screens (or ``shared``)."""

    attempt: int
    errors: int
    screens: dict[str, int]


@dataclass
class SwiftUIResult:
    """Outcome of one SwiftUI generation."""

    app_dir: Path
    scheme: str
    plan: NavPlan
    derived_data: Path
    errors: list[str] = field(default_factory=list)
    build_log: Path | None = None
    contract_restored: list[str] = field(default_factory=list)
    contract_removed: list[str] = field(default_factory=list)
    tasks: list[TaskRun] = field(default_factory=list)
    fix_rounds: list[FixRound] = field(default_factory=list)

    @property
    def entries(self) -> list[ScreenEntry]:
        return self.plan.entries


def _workspace_app(paths: RunPaths) -> Path:
    return paths.claude_ws / APP_DIR


def _derived_data(paths: RunPaths) -> Path:
    return paths.run_dir / "DerivedData"


def prompter_names(spec: dict[str, Any]) -> list[str]:
    """Type names of the prompters the scaffold generates for ``spec``."""
    return [PROMPTERS[k.case].type_name for k in declared_kinds(spec) if k.case in PROMPTERS]


def prepare_workspace(paths: RunPaths, *, app_name: str, bundle_id: str) -> NavPlan:
    """Stage inputs + reference into ``claude_ws`` and render the scaffold there."""
    ws = paths.claude_ws
    ws.mkdir(parents=True, exist_ok=True)
    shutil.copy2(paths.app_spec_json, ws / "app_spec.json")
    if paths.screens_dir.exists():
        shutil.copytree(paths.screens_dir, ws / "screens", dirs_exist_ok=True)
    stage_archive_context(paths, ws, include_bytes=True)
    if REFERENCE_DIR.is_dir():
        shutil.copytree(
            REFERENCE_DIR,
            ws / "reference",
            dirs_exist_ok=True,
            ignore=shutil.ignore_patterns("build", "*.xcodeproj", "Info.plist", "*.png"),
        )
    spec = json.loads(paths.app_spec_json.read_text())
    write_scaffold(
        _workspace_app(paths),
        spec,
        app_name=app_name,
        bundle_id=bundle_id,
        fonts_dir=paths.fonts_dir,
        media_dir=paths.media_dir,
    )
    return build_plan(spec)


def _tab_bar_errors(app_dir: Path) -> list[str]:
    path = app_dir / "App" / "Components" / "AppTabBar.swift"
    if path.is_file() and re.search(r"\bRouter\b", path.read_text(encoding="utf-8")):
        return [
            "App/Components/AppTabBar.swift: error: AppTabBar must only assign `selection` — "
            "it may not reference Router"
        ]
    return []


def compile_errors(
    app_dir: Path, spec: dict[str, Any], identity: AppIdentity, derived_data: Path
) -> GateCheck:
    """Enforce the contract, then contract/permission checks + xcodebuild (skipped off-Mac)."""
    report = enforce_contract(app_dir, spec, identity)
    if report.tampered:
        log.warning(
            "swiftui_gen.contract_enforced", restored=report.restored, removed=report.removed
        )
    errors = [
        f"{path}: error: unexpected path — move this code into App/Theme, App/Components, "
        "App/Features/<screen id> or App/Fixtures"
        for path in report.unexpected
    ]
    errors += _tab_bar_errors(app_dir)
    errors += permission_violations(app_dir, declared=set(prompter_names(spec)))
    if not xcode.toolchain_available():
        log.warning("swiftui_gen.toolchain_missing", app_dir=str(app_dir))
        return GateCheck(errors, contract=report)
    try:
        xcode.generate_project(app_dir)
    except xcode.XcodeError as exc:
        return GateCheck([*errors, str(exc)], contract=report)
    outcome = xcode.build(app_dir, target_name(identity.app_name), derived_data=derived_data)
    return GateCheck([*outcome.errors, *errors], outcome.log, report)


def screen_of_error(error: str, plan: NavPlan) -> str:
    """Screen id an error belongs to (by file path), else ``shared``."""
    feature = _FEATURE_PATH.search(error)
    if feature:
        return feature.group(1)
    fixtures = _FIXTURES_PATH.search(error)
    if fixtures:
        for entry in plan.entries:
            if entry.fixtures_path.endswith(fixtures.group(1)):
                return entry.screen_id
    return SHARED


def _attribute(errors: list[str], plan: NavPlan) -> dict[str, int]:
    counts: dict[str, int] = {}
    for error in errors:
        owner = screen_of_error(error, plan)
        counts[owner] = counts.get(owner, 0) + 1
    return counts


def ensure_compiles(
    paths: RunPaths,
    identity: AppIdentity,
    plan: NavPlan,
    *,
    prompters: list[str],
    attempts: int = 3,
    timeout: int = 1800,
) -> tuple[GateCheck, list[FixRound]]:
    """Compile gate with a bounded fix loop; returns the last check (contract = all passes)."""
    bound = log.bind(stage="compile_gate", run_dir=str(paths.run_dir), target="swiftui")
    app_dir = _workspace_app(paths)
    spec = json.loads(paths.app_spec_json.read_text())
    check = compile_errors(app_dir, spec, identity, _derived_data(paths))
    total = ContractReport(list(check.contract.restored), list(check.contract.removed))
    rounds: list[FixRound] = []
    while check.errors and len(rounds) < attempts:
        rounds.append(FixRound(len(rounds) + 1, len(check.errors), _attribute(check.errors, plan)))
        bound.warning(
            "compile_gate.errors",
            attempt=len(rounds),
            count=len(check.errors),
            screens=rounds[-1].screens,
            sample=check.errors[:5],
        )
        claude_gen.run_task(
            paths.claude_ws,
            compile_fix_prompt(check.errors[:80], prompters=prompters),
            timeout=timeout,
            tlog=bound,
        )
        check = compile_errors(app_dir, spec, identity, _derived_data(paths))
        total.restored += [p for p in check.contract.restored if p not in total.restored]
        total.removed += [p for p in check.contract.removed if p not in total.removed]
    bound.info(
        "compile_gate.done", ok=not check.errors, remaining=len(check.errors), rounds=len(rounds)
    )
    return GateCheck(check.errors, check.log, total), rounds


def task_graph(plan: NavPlan) -> list[dict[str, object]]:
    """Deterministic DAG: theme → components → every screen (independent of each other)."""
    tasks: list[dict[str, object]] = [
        {"id": TASK_THEME, "deps": []},
        {"id": TASK_COMPONENTS, "deps": [TASK_THEME]},
    ]
    tasks += [{"id": f"screen-{e.screen_id}", "deps": [TASK_COMPONENTS]} for e in plan.entries]
    return tasks


def _targets(entry: ScreenEntry, spec: dict[str, Any], plan: NavPlan) -> list[ScreenEntry]:
    by_id = {e.screen_id: e for e in plan.entries}
    screen: dict[str, Any] = next(
        (s for s in spec.get("screens", []) if str(s.get("id")) == entry.screen_id), {}
    )
    edges = [
        str(e.get("to"))
        for e in (spec.get("navigation") or {}).get("map", []) or []
        if isinstance(e, dict) and str(e.get("from")) == entry.screen_id
    ]
    ordered = dict.fromkeys([*(str(t) for t in screen.get("navigates_to", []) or []), *edges])
    return [by_id[t] for t in ordered if t in by_id and t != entry.screen_id]


def _observed(paths: RunPaths, entry: ScreenEntry) -> bool:
    return (paths.claude_ws / "screens" / f"{entry.screen_id}.png").is_file()


def _app_files(app_dir: Path) -> dict[str, bytes]:
    root = app_dir / "App"
    return {
        p.relative_to(app_dir).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()
    }


def _owned(entry: ScreenEntry, rel: str) -> bool:
    return rel.startswith(f"App/Features/{entry.screen_id}/") or rel == entry.fixtures_path


def _run_screen(paths: RunPaths, entry: ScreenEntry, prompt: str, *, timeout: int) -> TaskRun:
    """Run one screen task in a sandbox copy; harvest only the screen's owned paths.

    ``ignored`` lists what the model changed outside its owned paths, measured
    against the sandbox's own starting snapshot (other screens harvested in
    parallel meanwhile must not show up as this screen's writes).
    """
    sandbox = paths.run_dir / "sandboxes" / entry.screen_id
    if sandbox.exists():
        shutil.rmtree(sandbox)
    shutil.copytree(
        paths.claude_ws,
        sandbox,
        ignore=shutil.ignore_patterns("*.xcodeproj", claude_gen.TASK_LOG),
    )
    before = _app_files(sandbox / APP_DIR)
    started = time.monotonic()
    code = claude_gen.run_task(
        sandbox, prompt, timeout=timeout, tlog=log.bind(task=f"screen-{entry.screen_id}")
    )
    elapsed = time.monotonic() - started
    ws_app = _workspace_app(paths)
    after = _app_files(sandbox / APP_DIR)
    ignored: list[str] = []
    for rel, data in after.items():
        if _owned(entry, rel):
            target = ws_app / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        elif before.get(rel) != data:
            ignored.append(rel)
    ignored += [rel for rel in before if rel not in after and not _owned(entry, rel)]
    shutil.rmtree(sandbox)
    return TaskRun(f"screen-{entry.screen_id}", round(elapsed, 1), code, sorted(ignored))


def _run_shared(paths: RunPaths, task: str, prompt: str, *, timeout: int) -> TaskRun:
    started = time.monotonic()
    code = claude_gen.run_task(paths.claude_ws, prompt, timeout=timeout, tlog=log.bind(task=task))
    return TaskRun(task, round(time.monotonic() - started, 1), code)


def generate(
    paths: RunPaths,
    *,
    app_name: str,
    bundle_id: str,
    task_timeout: int = 1800,
    fix_attempts: int = 3,
    max_parallel: int = 4,
    diverge_content: bool = True,
) -> SwiftUIResult:
    """Generate the SwiftUI app for ``paths.app_spec_json`` into ``paths.xcode_app``."""
    bound = log.bind(stage="codegen", target="swiftui", run_dir=str(paths.run_dir))
    plan = prepare_workspace(paths, app_name=app_name, bundle_id=bundle_id)
    spec = json.loads(paths.app_spec_json.read_text())
    prompters = prompter_names(spec)
    by_task = {f"screen-{e.screen_id}": e for e in plan.entries}
    runs: list[TaskRun] = []

    for layer in topo_layers(task_graph(plan)):
        ids = [str(t["id"]) for t in layer]
        bound.info("swiftui_gen.layer.start", tasks=ids)
        if ids == [TASK_THEME]:
            prompt = theme_prompt(
                app_name=app_name, prompters=prompters, diverge_content=diverge_content
            )
            runs.append(_run_shared(paths, TASK_THEME, prompt, timeout=task_timeout))
        elif ids == [TASK_COMPONENTS]:
            prompt = components_prompt(plan, prompters=prompters, diverge_content=diverge_content)
            runs.append(_run_shared(paths, TASK_COMPONENTS, prompt, timeout=task_timeout))
        else:
            with ThreadPoolExecutor(max_workers=max(1, max_parallel)) as pool:
                futures = [
                    pool.submit(
                        _run_screen,
                        paths,
                        by_task[tid],
                        screen_prompt(
                            by_task[tid],
                            plan,
                            targets=_targets(by_task[tid], spec, plan),
                            prompters=prompters,
                            observed=_observed(paths, by_task[tid]),
                            diverge_content=diverge_content,
                        ),
                        timeout=task_timeout,
                    )
                    for tid in ids
                ]
                runs += [f.result() for f in futures]

    gate, rounds = ensure_compiles(
        paths,
        AppIdentity(app_name, bundle_id),
        plan,
        prompters=prompters,
        attempts=fix_attempts,
        timeout=task_timeout,
    )
    app_dir = _workspace_app(paths)
    errors = [
        *gate.errors,
        *(f"screen {sid} not generated" for sid in pending_screens(app_dir, plan.entries)),
    ]
    if paths.xcode_app.exists():
        shutil.rmtree(paths.xcode_app)
    shutil.copytree(app_dir, paths.xcode_app, ignore=shutil.ignore_patterns("*.xcodeproj"))
    log_path: Path | None = None
    if gate.log:
        log_path = paths.run_dir / "xcodebuild.log"
        log_path.write_text(gate.log, encoding="utf-8")
    result = SwiftUIResult(
        app_dir=paths.xcode_app,
        scheme=target_name(app_name),
        plan=plan,
        derived_data=_derived_data(paths),
        errors=errors,
        build_log=log_path,
        contract_restored=gate.contract.restored,
        contract_removed=gate.contract.removed,
        tasks=runs,
        fix_rounds=rounds,
    )
    bound.info("swiftui_gen.done", ok=not errors, errors=len(errors), screens=len(plan.entries))
    return result


def report(result: SwiftUIResult) -> dict[str, Any]:
    """JSON-friendly run report (tabs, tasks, fix rounds, contract enforcement, errors)."""
    return {
        "app_dir": str(result.app_dir),
        "scheme": result.scheme,
        "tabs": [
            {"case": t.case_name, "title": t.title, "root": t.root.screen_id}
            for t in result.plan.tabs
        ],
        "screens": [
            {"id": e.screen_id, "presentation": e.presentation, "tab_root": e.tab_root}
            for e in result.plan.entries
        ],
        "tasks": [asdict(t) for t in result.tasks],
        "fix_rounds": [asdict(r) for r in result.fix_rounds],
        "contract_restored": result.contract_restored,
        "contract_removed": result.contract_removed,
        "errors": result.errors,
    }


def _scope_to(paths: RunPaths, screen_ids: list[str]) -> None:
    spec = json.loads(paths.app_spec_json.read_text())
    screens = [
        ScreenScope(
            screen_id=str(s["id"]),
            name=str(s.get("name") or s["id"]),
            include=str(s["id"]) in screen_ids,
            reason="codegen scope",
        )
        for s in spec.get("screens", [])
    ]
    scope = ScopeDecision(
        status="approved",
        scope_mode="core",
        screens=screens,
        feasibility=FeasibilityReport(overall_verdict="native", summary="codegen scope"),
        counts=ScopeCounts.from_screens(screens),
        proposed_at=datetime.now(UTC),
    )
    apply_scope(paths, scope)


def _capture_stable(udid: str, out: Path, *, timeout_s: float = 6.0) -> Path:
    deadline = time.monotonic() + timeout_s
    previous = b""
    while True:
        time.sleep(0.4)
        xcode.screenshot(udid, out)
        current = out.read_bytes()
        if current == previous or time.monotonic() > deadline:
            return out
        previous = current


def verify_on_simulator(
    result: SwiftUIResult, *, udid: str, bundle_id: str, out_dir: Path
) -> dict[str, Path]:
    """Install the built app and screenshot every screen plus an unknown id."""
    xcode.boot(udid)
    xcode.pin_status_bar(udid)
    xcode.install(udid, xcode.built_app(result.derived_data, result.scheme))
    shots: dict[str, Path] = {}
    for screen_id in [*(e.screen_id for e in result.entries), UNKNOWN_SCREEN_ID]:
        xcode.launch_screen(udid, bundle_id, screen_id)
        shots[screen_id] = _capture_stable(udid, out_dir / f"{screen_id}.png")
    return shots


def main(argv: list[str] | None = None) -> int:
    """Drive the codegen by hand: real job inputs → scoped app → build → screenshots."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app-spec", type=Path, required=True)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--screen", action="append", dest="screens", help="default: all")
    parser.add_argument("--exclude", action="append", default=[], help="screen ids to drop")
    parser.add_argument("--bundle-id", required=True)
    parser.add_argument("--app-name")
    parser.add_argument("--out", type=Path, default=Path("runs"))
    parser.add_argument("--max-parallel", type=int, default=4)
    parser.add_argument("--udid", help="simulator to verify screen-id screenshots on")
    args = parser.parse_args(argv)

    if not xcode.toolchain_available():
        print("xcodegen / xcodebuild / xcrun not found: SwiftUI codegen only runs on a Mac worker")
        return 2
    paths = RunPaths.create(args.out)
    shutil.copy2(args.app_spec, paths.app_spec_json)
    frida_ingest.ingest_archive(args.archive, paths)
    spec = json.loads(paths.app_spec_json.read_text())
    wanted = args.screens or [str(s["id"]) for s in spec.get("screens", [])]
    _scope_to(paths, [sid for sid in wanted if sid not in set(args.exclude)])
    spec = json.loads(paths.app_spec_json.read_text())
    app_name = args.app_name or str(spec.get("app_name") or "Generated App")

    result = generate(
        paths, app_name=app_name, bundle_id=args.bundle_id, max_parallel=args.max_parallel
    )
    summary = report(result)
    (paths.run_dir / "report.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False))
    print(json.dumps({"run_dir": str(paths.run_dir), "errors": result.errors}, indent=2))
    if result.errors:
        return 1
    if args.udid:
        shots = verify_on_simulator(
            result, udid=args.udid, bundle_id=args.bundle_id, out_dir=paths.generated_screens_dir
        )
        print(json.dumps({k: str(v) for k, v in shots.items()}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
