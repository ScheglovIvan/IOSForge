"""SwiftUI codegen (Phase 3, thin vertical slice): scaffold → theme → screens → compile gate.

The deterministic scaffold (:mod:`iosforge.mvp.swiftui_scaffold`) hard-wires the
screen-id contract; the local Claude Code CLI then fills the theme and one screen
task per ``app_spec`` screen through :func:`iosforge.mvp.claude_gen.run_task`. The
compile gate runs XcodeGen + ``xcodebuild`` (generic iOS Simulator) plus the
permission lint and loops a bounded fix task, mirroring the Flutter
``claude_gen.ensure_compiles``. Not yet wired into ``run_job`` (behind the future
``codegen_target`` setting); the ``main`` entry point drives the slice by hand.
"""

from __future__ import annotations

import argparse
import json
import shutil
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from iosforge.common.logging import get_logger
from iosforge.mvp import claude_gen, frida_ingest, xcode
from iosforge.mvp.analyze import stage_archive_context
from iosforge.mvp.feasibility import apply_scope
from iosforge.mvp.paths import RunPaths
from iosforge.mvp.scope_models import FeasibilityReport, ScopeCounts, ScopeDecision, ScreenScope
from iosforge.mvp.swiftui_permissions import permission_violations
from iosforge.mvp.swiftui_prompts import APP_DIR, compile_fix_prompt, screen_prompt, theme_prompt
from iosforge.mvp.swiftui_scaffold import (
    ScreenEntry,
    pending_screens,
    target_name,
    write_scaffold,
)

log = get_logger("mvp.swiftui_gen")

REFERENCE_DIR = Path(__file__).resolve().parents[2] / "docs" / "swiftui-reference"
UNKNOWN_SCREEN_ID = "__unknown__"


@dataclass
class SwiftUIResult:
    """Outcome of one SwiftUI generation: project, screens, remaining errors, build log."""

    app_dir: Path
    scheme: str
    entries: list[ScreenEntry]
    errors: list[str] = field(default_factory=list)
    build_log: Path | None = None


def _workspace_app(paths: RunPaths) -> Path:
    return paths.claude_ws / APP_DIR


def _derived_data(paths: RunPaths) -> Path:
    return paths.run_dir / "DerivedData"


def prepare_workspace(paths: RunPaths, *, app_name: str, bundle_id: str) -> list[ScreenEntry]:
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
    return write_scaffold(
        _workspace_app(paths),
        spec,
        app_name=app_name,
        bundle_id=bundle_id,
        fonts_dir=paths.fonts_dir,
        media_dir=paths.media_dir,
    )


def compile_errors(app_dir: Path, scheme: str, derived_data: Path) -> tuple[list[str], str]:
    """Permission-lint violations plus xcodebuild errors (build skipped off-Mac)."""
    errors = permission_violations(app_dir)
    if not xcode.toolchain_available():
        log.warning("swiftui_gen.toolchain_missing", app_dir=str(app_dir))
        return errors, ""
    try:
        xcode.generate_project(app_dir)
    except xcode.XcodeError as exc:
        return [*errors, str(exc)], ""
    outcome = xcode.build(app_dir, scheme, derived_data=derived_data)
    return [*outcome.errors, *errors], outcome.log


def ensure_compiles(
    paths: RunPaths,
    scheme: str,
    *,
    attempts: int = 2,
    timeout: int = 1800,
) -> tuple[list[str], str]:
    """Compile gate with a bounded fix loop; returns (remaining errors, last build log)."""
    bound = log.bind(stage="compile_gate", run_dir=str(paths.run_dir), target="swiftui")
    app_dir = _workspace_app(paths)
    errors, build_log = compile_errors(app_dir, scheme, _derived_data(paths))
    tries = 0
    while errors and tries < attempts:
        tries += 1
        bound.warning("compile_gate.errors", attempt=tries, count=len(errors), sample=errors[:5])
        claude_gen.run_task(
            paths.claude_ws,
            compile_fix_prompt(errors[:80]),
            timeout=timeout,
            tlog=bound,
        )
        errors, build_log = compile_errors(app_dir, scheme, _derived_data(paths))
    bound.info("compile_gate.done", ok=not errors, remaining=len(errors), attempts=tries)
    return errors, build_log


def _targets(
    entry: ScreenEntry, spec: dict[str, Any], entries: list[ScreenEntry]
) -> list[ScreenEntry]:
    by_id = {e.screen_id: e for e in entries}
    screen: dict[str, Any] = next(
        (s for s in spec.get("screens", []) if str(s.get("id")) == entry.screen_id), {}
    )
    return [by_id[str(t)] for t in screen.get("navigates_to", []) or [] if str(t) in by_id]


def generate(
    paths: RunPaths,
    *,
    app_name: str,
    bundle_id: str,
    task_timeout: int = 1800,
    fix_attempts: int = 2,
    diverge_content: bool = True,
) -> SwiftUIResult:
    """Generate the SwiftUI app for ``paths.app_spec_json`` into ``paths.xcode_app``."""
    bound = log.bind(stage="codegen", target="swiftui", run_dir=str(paths.run_dir))
    entries = prepare_workspace(paths, app_name=app_name, bundle_id=bundle_id)
    scheme = target_name(app_name)
    spec = json.loads(paths.app_spec_json.read_text())

    bound.info("swiftui_gen.theme.start")
    claude_gen.run_task(
        paths.claude_ws,
        theme_prompt(app_name=app_name, diverge_content=diverge_content),
        timeout=task_timeout,
        tlog=bound.bind(task="theme"),
    )
    for entry in entries:
        bound.info("swiftui_gen.screen.start", screen=entry.screen_id)
        claude_gen.run_task(
            paths.claude_ws,
            screen_prompt(
                entry,
                targets=_targets(entry, spec, entries),
                diverge_content=diverge_content,
            ),
            timeout=task_timeout,
            tlog=bound.bind(task=f"screen-{entry.screen_id}"),
        )

    errors, build_log = ensure_compiles(paths, scheme, attempts=fix_attempts, timeout=task_timeout)
    app_dir = _workspace_app(paths)
    errors += [f"screen {sid} not generated" for sid in pending_screens(app_dir, entries)]

    if paths.xcode_app.exists():
        shutil.rmtree(paths.xcode_app)
    shutil.copytree(app_dir, paths.xcode_app, ignore=shutil.ignore_patterns("*.xcodeproj"))
    log_path: Path | None = None
    if build_log:
        log_path = paths.run_dir / "xcodebuild.log"
        log_path.write_text(build_log, encoding="utf-8")
    bound.info("swiftui_gen.done", ok=not errors, errors=len(errors), screens=len(entries))
    return SwiftUIResult(paths.xcode_app, scheme, entries, errors, log_path)


def _scope_to(paths: RunPaths, screen_ids: list[str]) -> None:
    spec = json.loads(paths.app_spec_json.read_text())
    screens = [
        ScreenScope(
            screen_id=str(s["id"]),
            name=str(s.get("name") or s["id"]),
            include=str(s["id"]) in screen_ids,
            reason="slice",
        )
        for s in spec.get("screens", [])
    ]
    scope = ScopeDecision(
        status="approved",
        scope_mode="core",
        screens=screens,
        feasibility=FeasibilityReport(overall_verdict="native", summary="slice"),
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
    xcode.install(udid, xcode.built_app(result.app_dir.parent / "DerivedData", result.scheme))
    shots: dict[str, Path] = {}
    for screen_id in [*(e.screen_id for e in result.entries), UNKNOWN_SCREEN_ID]:
        xcode.launch_screen(udid, bundle_id, screen_id)
        shots[screen_id] = _capture_stable(udid, out_dir / f"{screen_id}.png")
    return shots


def main(argv: list[str] | None = None) -> int:
    """Drive the slice by hand: real job inputs → scoped app → build → screenshots."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app-spec", type=Path, required=True)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--screen", action="append", required=True, dest="screens")
    parser.add_argument("--bundle-id", required=True)
    parser.add_argument("--app-name")
    parser.add_argument("--out", type=Path, default=Path("runs"))
    parser.add_argument("--udid", help="simulator to verify screen-id screenshots on")
    args = parser.parse_args(argv)

    paths = RunPaths.create(args.out)
    shutil.copy2(args.app_spec, paths.app_spec_json)
    frida_ingest.ingest_archive(args.archive, paths)
    _scope_to(paths, args.screens)
    spec = json.loads(paths.app_spec_json.read_text())
    app_name = args.app_name or str(spec.get("app_name") or "Generated App")

    result = generate(paths, app_name=app_name, bundle_id=args.bundle_id)
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
