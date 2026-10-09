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
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from iosforge.common.config import Settings
from iosforge.common.logging import get_logger
from iosforge.mvp import claude_gen, frida_ingest, simulator, swiftui_media, xcode
from iosforge.mvp import swiftui_integrations as integ
from iosforge.mvp.analyze import stage_archive_context, topo_layers
from iosforge.mvp.feasibility import apply_scope
from iosforge.mvp.paths import RunPaths
from iosforge.mvp.scope_models import FeasibilityReport, ScopeCounts, ScopeDecision, ScreenScope
from iosforge.mvp.swiftui_ads import ad_violations, strip_ad_components
from iosforge.mvp.swiftui_permissions import blank_comments_and_strings, permission_violations
from iosforge.mvp.swiftui_prompters import PROMPTERS
from iosforge.mvp.swiftui_prompts import (
    APP_DIR,
    COMPONENTS_MD,
    compile_fix_prompt,
    components_prompt,
    corrective_prompt,
    rework_prompt,
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

_SANDBOX_LOCK = threading.Lock()
_OWN_NAVIGATION = re.compile(
    r"\b(?:NavigationStack|NavigationView|TabView)\b|\.(?:sheet|fullScreenCover)\s*\(|"
    r"NavigationLink\s*\(\s*destination"
)
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


def prompter_names(spec: dict[str, Any], app_dir: Path | None = None) -> list[str]:
    """Type names of the prompters the scaffold generates for ``spec`` (and ``app_dir``).

    Attribution adds the ATT prompter even when the spec does not declare tracking.
    """
    cases = [k.case for k in declared_kinds(spec)]
    if app_dir is not None and integ.load(app_dir).attribution and "tracking" not in cases:
        cases.append("tracking")
    return [PROMPTERS[case].type_name for case in cases if case in PROMPTERS]


def prepare_workspace(paths: RunPaths, *, app_name: str, bundle_id: str) -> NavPlan:
    """Stage inputs + reference into ``claude_ws`` and render the scaffold there."""
    ws = paths.claude_ws
    ws.mkdir(parents=True, exist_ok=True)
    spec = json.loads(paths.app_spec_json.read_text(encoding="utf-8"))
    model_spec, ad_components = strip_ad_components(spec)
    (ws / "app_spec.json").write_text(
        json.dumps(model_spec, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    log.info("swiftui_gen.ads_stripped", components=ad_components)
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
    write_scaffold(
        _workspace_app(paths),
        spec,
        app_name=app_name,
        bundle_id=bundle_id,
        fonts_dir=paths.fonts_dir,
        media_dir=paths.media_dir,
    )
    return build_plan(spec)


def _navigation_errors(app_dir: Path) -> list[str]:
    errors: list[str] = []
    for folder in ("App/Features", "App/Components"):
        for swift in sorted((app_dir / folder).rglob("*.swift")):
            source = blank_comments_and_strings(swift.read_text(encoding="utf-8", errors="replace"))
            for match in _OWN_NAVIGATION.finditer(source):
                line = source.count("\n", 0, match.start()) + 1
                errors.append(
                    f"{swift.relative_to(app_dir).as_posix()}:{line}: error: own navigation "
                    f"`{match.group(0).strip()}` — navigate only through the scaffold Router "
                    "(router.show / router.dismiss); for paged carousels use ScrollView + "
                    "`.scrollTargetBehavior(.paging)` instead of TabView"
                )
    return errors


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
    errors += _navigation_errors(app_dir)
    errors += permission_violations(app_dir, declared=set(prompter_names(spec, app_dir)))
    errors += ad_violations(app_dir)
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


def _screen_owner(entry: ScreenEntry) -> Callable[[str], bool]:
    def owned(rel: str) -> bool:
        return rel.startswith(f"App/Features/{entry.screen_id}/") or rel == entry.fixtures_path

    return owned


def _dir_owner(prefix: str) -> Callable[[str], bool]:
    def owned(rel: str) -> bool:
        return rel.startswith(prefix)

    return owned


def _run_sandboxed(
    paths: RunPaths,
    task: str,
    prompt: str,
    owned: Callable[[str], bool],
    *,
    timeout: int,
    root_files: tuple[str, ...] = (),
) -> TaskRun:
    """Run one model task in a sandbox copy of the workspace; harvest only owned paths.

    Copying into and harvesting out of sandboxes is serialised so a sandbox never
    sees a half-written file of a task finishing in parallel. Owned files the model
    created or changed are copied back, owned files it deleted are deleted;
    ``ignored`` lists its changes outside the owned paths, measured against the
    sandbox's own starting snapshot. ``root_files`` are workspace-root files the
    task owns (e.g. ``COMPONENTS.md``).
    """
    sandbox = paths.run_dir / "sandboxes" / task
    with _SANDBOX_LOCK:
        if sandbox.exists():
            shutil.rmtree(sandbox)
        shutil.copytree(
            paths.claude_ws,
            sandbox,
            ignore=shutil.ignore_patterns("*.xcodeproj", claude_gen.TASK_LOG),
        )
    before = _app_files(sandbox / APP_DIR)
    started = time.monotonic()
    code = claude_gen.run_task(sandbox, prompt, timeout=timeout, tlog=log.bind(task=task))
    elapsed = time.monotonic() - started
    ws_app = _workspace_app(paths)
    after = _app_files(sandbox / APP_DIR)
    ignored: list[str] = []
    with _SANDBOX_LOCK:
        for rel, data in after.items():
            if owned(rel):
                target = ws_app / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
            elif before.get(rel) != data:
                ignored.append(rel)
        for rel in before:
            if rel not in after:
                if owned(rel):
                    (ws_app / rel).unlink(missing_ok=True)
                else:
                    ignored.append(rel)
        for name in root_files:
            if (sandbox / name).is_file():
                shutil.copy2(sandbox / name, paths.claude_ws / name)
        shutil.rmtree(sandbox)
    return TaskRun(task, round(elapsed, 1), code, sorted(ignored))


def generate(
    paths: RunPaths,
    *,
    app_name: str,
    bundle_id: str,
    task_timeout: int = 1800,
    fix_attempts: int = 3,
    max_parallel: int = 4,
    diverge_content: bool = True,
    settings: Settings | None = None,
) -> SwiftUIResult:
    """Generate the SwiftUI app for ``paths.app_spec_json`` into ``paths.xcode_app``.

    With ``settings`` the decorative image slots get replacement images first
    (:func:`iosforge.mvp.swiftui_media.generate_images`).
    """
    bound = log.bind(stage="codegen", target="swiftui", run_dir=str(paths.run_dir))
    plan = prepare_workspace(paths, app_name=app_name, bundle_id=bundle_id)
    spec = json.loads(paths.app_spec_json.read_text())
    images: dict[str, list[tuple[str, str]]] = {}
    if settings is not None:
        model_spec = json.loads((paths.claude_ws / "app_spec.json").read_text(encoding="utf-8"))
        for image in swiftui_media.generate_images(
            _workspace_app(paths), paths.claude_ws, model_spec, settings=settings
        ):
            images.setdefault(image.screen_id, []).append((image.file, image.description))
    prompters = prompter_names(spec, _workspace_app(paths))
    by_task = {f"screen-{e.screen_id}": e for e in plan.entries}
    runs: list[TaskRun] = []

    for layer in topo_layers(task_graph(plan)):
        ids = [str(t["id"]) for t in layer]
        bound.info("swiftui_gen.layer.start", tasks=ids)
        if ids == [TASK_THEME]:
            prompt = theme_prompt(
                app_name=app_name, prompters=prompters, diverge_content=diverge_content
            )
            runs.append(
                _run_sandboxed(
                    paths, TASK_THEME, prompt, _dir_owner("App/Theme/"), timeout=task_timeout
                )
            )
        elif ids == [TASK_COMPONENTS]:
            prompt = components_prompt(plan, prompters=prompters, diverge_content=diverge_content)
            runs.append(
                _run_sandboxed(
                    paths,
                    TASK_COMPONENTS,
                    prompt,
                    _dir_owner("App/Components/"),
                    timeout=task_timeout,
                    root_files=(COMPONENTS_MD,),
                )
            )
        else:
            with ThreadPoolExecutor(max_workers=max(1, max_parallel)) as pool:
                futures = [
                    pool.submit(
                        _run_sandboxed,
                        paths,
                        tid,
                        screen_prompt(
                            by_task[tid],
                            plan,
                            targets=_targets(by_task[tid], spec, plan),
                            prompters=prompters,
                            observed=_observed(paths, by_task[tid]),
                            diverge_content=diverge_content,
                            images=images.get(by_task[tid].screen_id),
                        ),
                        _screen_owner(by_task[tid]),
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


_DISPLAY_NAME = re.compile(r"CFBundleDisplayName:\s*(\".*\")")
_BUNDLE_ID = re.compile(r"PRODUCT_BUNDLE_IDENTIFIER:\s*(\S+)")


def identity_from_project(app_dir: Path) -> AppIdentity:
    """App name and bundle id the scaffold rendered into ``app_dir/project.yml``."""
    text = (app_dir / "project.yml").read_text(encoding="utf-8")
    name, bundle = _DISPLAY_NAME.search(text), _BUNDLE_ID.search(text)
    if name is None or bundle is None:
        raise RuntimeError(f"{app_dir}/project.yml lacks CFBundleDisplayName / bundle id")
    return AppIdentity(str(json.loads(name.group(1))), bundle.group(1))


def restore_workspace(paths: RunPaths) -> None:
    """Make ``claude_ws`` mirror the current ``xcode_app`` plus inputs and latest renders."""
    ws = paths.claude_ws
    ws.mkdir(parents=True, exist_ok=True)
    if not (ws / "app_spec.json").exists():
        spec = json.loads(paths.app_spec_json.read_text(encoding="utf-8"))
        model_spec, _ = strip_ad_components(spec)
        (ws / "app_spec.json").write_text(
            json.dumps(model_spec, indent=2, ensure_ascii=False), encoding="utf-8"
        )
    for name, src in (
        ("screens", paths.screens_dir),
        ("generated_screens", paths.generated_screens_dir),
    ):
        if src.exists():
            if (ws / name).exists():
                shutil.rmtree(ws / name)
            shutil.copytree(src, ws / name)
    if not (ws / "source").exists():
        stage_archive_context(paths, ws, include_bytes=True)
    if REFERENCE_DIR.is_dir() and not (ws / "reference").exists():
        shutil.copytree(
            REFERENCE_DIR,
            ws / "reference",
            ignore=shutil.ignore_patterns("build", "*.xcodeproj", "Info.plist", "*.png"),
        )
    if _workspace_app(paths).exists():
        shutil.rmtree(_workspace_app(paths))
    shutil.copytree(
        paths.xcode_app, _workspace_app(paths), ignore=shutil.ignore_patterns("*.xcodeproj")
    )


def _task_screen(task: dict[str, Any]) -> str | None:
    screens = task.get("screens")
    if isinstance(screens, list) and screens:
        return str(screens[0])
    if task.get("from"):
        return str(task["from"])
    return None


def correct(
    paths: RunPaths,
    tasks: list[dict[str, Any]],
    *,
    max_parallel: int = 4,
    timeout: int = 1800,
    fix_attempts: int = 3,
) -> list[TaskRun]:
    """Apply Vision-Judge corrective tasks to ``paths.xcode_app`` (one sandbox per screen).

    Tasks are grouped by screen; each group runs as a screen-owned sandbox task (only that
    screen's files are kept), then the compile gate restores the contract and fixes build
    errors, and the result replaces ``paths.xcode_app``. Raises when the app no longer builds.
    """
    bound = log.bind(stage="compliance.correct", target="swiftui", run_dir=str(paths.run_dir))
    spec = json.loads(paths.app_spec_json.read_text(encoding="utf-8"))
    plan = build_plan(spec)
    by_id = {e.screen_id: e for e in plan.entries}
    prompters = prompter_names(spec, paths.xcode_app)
    groups: dict[str, list[dict[str, Any]]] = {}
    for task in tasks:
        sid = _task_screen(task)
        if sid not in by_id:
            bound.warning("compliance.correct.unknown_screen", task=task.get("id"), screen=sid)
            continue
        if task.get("type") == "add_edge" and str(task.get("to")) in by_id:
            task = {**task, "to_case": by_id[str(task["to"])].case_name}
        groups.setdefault(str(sid), []).append(task)
    if not groups:
        return []
    identity = identity_from_project(paths.xcode_app)
    restore_workspace(paths)
    with ThreadPoolExecutor(max_workers=max(1, max_parallel)) as pool:
        futures = [
            pool.submit(
                _run_sandboxed,
                paths,
                f"fix-{sid}",
                corrective_prompt(group, by_id[sid], prompters=prompters),
                _screen_owner(by_id[sid]),
                timeout=timeout,
            )
            for sid, group in groups.items()
        ]
        runs = [f.result() for f in futures]
    gate, _ = ensure_compiles(
        paths, identity, plan, prompters=prompters, attempts=fix_attempts, timeout=timeout
    )
    if gate.errors:
        raise RuntimeError(f"corrective round left a non-compiling app: {gate.errors[:10]}")
    _swap_in(paths)
    bound.info("compliance.correct.done", screens=sorted(groups), tasks=len(tasks))
    return runs


_MODEL_PREFIXES = ("App/Theme/", "App/Components/", "App/Features/", "App/Fixtures/")


def _model_owner(rel: str) -> bool:
    return rel.startswith(_MODEL_PREFIXES)


def _swap_in(paths: RunPaths) -> None:
    staged = paths.run_dir / "xcode_app.next"
    if staged.exists():
        shutil.rmtree(staged)
    shutil.copytree(_workspace_app(paths), staged, ignore=shutil.ignore_patterns("*.xcodeproj"))
    shutil.rmtree(paths.xcode_app)
    staged.rename(paths.xcode_app)


def rework(
    paths: RunPaths,
    instructions: str,
    *,
    timeout: int = 5400,
    fix_attempts: int = 3,
) -> list[TaskRun]:
    """One operator rework round over ``paths.xcode_app`` (model-owned code only).

    Runs a sandboxed task that may touch theme, components, screens and fixtures; the
    compile gate then restores the contract and fixes build errors. Raises when the app
    no longer builds, leaving ``paths.xcode_app`` unchanged.
    """
    spec = json.loads(paths.app_spec_json.read_text(encoding="utf-8"))
    plan = build_plan(spec)
    identity = identity_from_project(paths.xcode_app)
    prompters = prompter_names(spec, paths.xcode_app)
    restore_workspace(paths)
    run = _run_sandboxed(
        paths,
        "rework",
        rework_prompt(instructions, prompters=prompters),
        _model_owner,
        timeout=timeout,
        root_files=(COMPONENTS_MD,),
    )
    gate, _ = ensure_compiles(
        paths, identity, plan, prompters=prompters, attempts=fix_attempts, timeout=timeout
    )
    if gate.errors:
        raise RuntimeError(f"rework round left a non-compiling app: {gate.errors[:10]}")
    _swap_in(paths)
    return [run]


def extend(
    paths: RunPaths,
    screen_ids: list[str],
    full_spec: dict[str, Any],
    *,
    timeout: int = 1800,
    fix_attempts: int = 3,
    max_parallel: int = 4,
) -> list[TaskRun]:
    """Grow the app's scope with ``screen_ids`` from ``full_spec`` (post-MVP extension).

    Re-scopes the spec to current + requested screens, re-renders the contract (new
    ``ScreenID`` cases, tabs, prompters) around the existing model code, generates only
    the new screens in sandboxes and runs the compile gate. Unknown ids are ignored.
    """
    current = {
        str(s.get("id"))
        for s in json.loads(paths.app_spec_json.read_text(encoding="utf-8")).get("screens", [])
    }
    known = {str(s.get("id")) for s in full_spec.get("screens", []) if isinstance(s, dict)}
    added = [sid for sid in screen_ids if sid in known and sid not in current]
    if not added:
        return []
    paths.app_spec_json.write_text(json.dumps(full_spec, ensure_ascii=False), encoding="utf-8")
    scope_to(paths, sorted(current | set(added)))
    spec = json.loads(paths.app_spec_json.read_text(encoding="utf-8"))
    identity = identity_from_project(paths.xcode_app)
    write_scaffold(paths.xcode_app, spec, app_name=identity.app_name, bundle_id=identity.bundle_id)
    model_spec, _ = strip_ad_components(spec)
    paths.claude_ws.mkdir(parents=True, exist_ok=True)
    (paths.claude_ws / "app_spec.json").write_text(
        json.dumps(model_spec, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    restore_workspace(paths)
    plan = build_plan(spec)
    by_id = {e.screen_id: e for e in plan.entries}
    prompters = prompter_names(spec, paths.xcode_app)
    with ThreadPoolExecutor(max_workers=max(1, max_parallel)) as pool:
        futures = [
            pool.submit(
                _run_sandboxed,
                paths,
                f"screen-{sid}",
                screen_prompt(
                    by_id[sid],
                    plan,
                    targets=_targets(by_id[sid], spec, plan),
                    prompters=prompters,
                    observed=_observed(paths, by_id[sid]),
                ),
                _screen_owner(by_id[sid]),
                timeout=timeout,
            )
            for sid in added
            if sid in by_id
        ]
        runs = [f.result() for f in futures]
    gate, _ = ensure_compiles(
        paths, identity, plan, prompters=prompters, attempts=fix_attempts, timeout=timeout
    )
    pending = pending_screens(_workspace_app(paths), plan.entries)
    if gate.errors or pending:
        raise RuntimeError(f"scope extension incomplete: {[*gate.errors[:10], *pending]}")
    _swap_in(paths)
    return runs


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


def scope_to(paths: RunPaths, screen_ids: list[str]) -> None:
    """Prune ``app_spec.json`` (scope mechanism) and the judge's originals to ``screen_ids``.

    ``screens.json`` lists the originals the Vision Judge scores; an excluded screen left
    there would count as "not rendered". The full crawl is kept as ``screens_full.json``
    and the unpruned spec as ``app_spec_full.json`` (scope extensions start from it).
    """
    full_spec = paths.run_dir / "app_spec_full.json"
    if not full_spec.exists() and paths.app_spec_json.is_file():
        shutil.copy2(paths.app_spec_json, full_spec)
    if paths.screens_json.is_file():
        full = paths.run_dir / "screens_full.json"
        if not full.exists():
            shutil.copy2(paths.screens_json, full)
        crawl = json.loads(full.read_text(encoding="utf-8"))
        if isinstance(crawl, dict):
            crawl["screens"] = [
                s for s in crawl.get("screens", []) if str(s.get("id")) in set(screen_ids)
            ]
            paths.screens_json.write_text(json.dumps(crawl, ensure_ascii=False), encoding="utf-8")
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


def verify_on_simulator(
    result: SwiftUIResult, *, udid: str, bundle_id: str, out_dir: Path, locale: str = "en-US"
) -> dict[str, Path]:
    """Install the built app and screenshot every screen plus an unknown id (stable frames)."""
    env = simulator.SimEnvironment(udid=udid, locale=locale)
    simulator.pin_environment(env)
    xcode.install(udid, xcode.built_app(result.derived_data, result.scheme))
    return {
        sid: simulator.render_screen(env, bundle_id, sid, out_dir / f"{sid}.png").path
        for sid in [*(e.screen_id for e in result.entries), UNKNOWN_SCREEN_ID]
    }


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
    scope_to(paths, [sid for sid in wanted if sid not in set(args.exclude)])
    spec = json.loads(paths.app_spec_json.read_text())
    app_name = args.app_name or str(spec.get("app_name") or "Generated App")

    result = generate(
        paths, app_name=app_name, bundle_id=args.bundle_id, max_parallel=args.max_parallel
    )
    summary = report(result)
    (paths.run_dir / "report.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8"
    )
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
