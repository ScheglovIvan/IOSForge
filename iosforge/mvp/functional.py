"""Functional verification of a generated SwiftUI app against its capability mocks.

For every functional check of the app's capability modules
(:func:`iosforge.mvp.swiftui_capabilities.functional_checks`): start the module's mock
(a local stub server, a seeded library, a fake receiver — registered in
:data:`MOCKS`), run the app's ``FunctionalUITests`` live on the simulator (not
headless) and read the module journal back from the app container. A check passes
only when its UI flow passed AND every expected effect was recorded by the module
during that check. The report goes to ``functional_report.json`` and into the Vision
Judge's structural audit.

This proves the integration against the MOCK, not against real hardware or a real
backend (DECISIONS 2026-10-10 «Level 2»).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from contextlib import AbstractContextManager, ExitStack
from typing import Any

from iosforge.common.logging import get_logger
from iosforge.mvp import simulator, xcode
from iosforge.mvp import swiftui_capabilities as caps
from iosforge.mvp import swiftui_integrations as integ
from iosforge.mvp.paths import RunPaths
from iosforge.mvp.swiftui_functional import JOURNAL_NAME, UI_TEST_TARGET, FunctionalCheck

log = get_logger("mvp.functional")

REPORT_NAME = "functional_report.json"

MockFactory = Callable[[FunctionalCheck], AbstractContextManager[dict[str, str]]]

#: Mock name → context manager yielding the ``IOSFORGE_*`` settings the app needs.
MOCKS: dict[str, MockFactory] = {}


def register_mock(name: str, factory: MockFactory) -> MockFactory:
    """Make a mock available to the functional checks that name it."""
    MOCKS[name] = factory
    return factory


def app_checks(paths: RunPaths, spec: dict[str, Any]) -> list[FunctionalCheck]:
    """Functional checks of the app built from ``spec`` (none: functional step skipped)."""
    return caps.functional_checks(caps.select(spec, integ.load(paths.xcode_app)))


def read_journal(udid: str, bundle_id: str) -> list[dict[str, str]]:
    """Effects the capability modules recorded in the app container (empty when none)."""
    container = xcode.app_data_container(udid, bundle_id)
    journal = container / "Documents" / JOURNAL_NAME if container else None
    if journal is None or not journal.is_file():
        return []
    events: list[dict[str, str]] = []
    for line in journal.read_text(encoding="utf-8").splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict):
            events.append({str(k): str(v) for k, v in entry.items()})
    return events


def clear_journal(udid: str, bundle_id: str) -> None:
    """Drop a previous run's journal so this run's evidence stands alone."""
    container = xcode.app_data_container(udid, bundle_id)
    if container is not None:
        (container / "Documents" / JOURNAL_NAME).unlink(missing_ok=True)


def verdict(
    check: FunctionalCheck, outcome: xcode.UITestOutcome, events: list[dict[str, str]]
) -> dict[str, Any]:
    """One check's result: UI flow passed and every expected effect recorded (pure)."""
    seen = [e["event"] for e in events if e.get("check") == check.name]
    missing = [event for event in check.expect_events if event not in seen]
    ui_passed = check.test_name in outcome.passed
    reason = outcome.failed.get(check.test_name) or outcome.failed.get("xcodebuild", "")
    if not ui_passed and not reason:
        reason = "the UI test did not run"
    if ui_passed and missing:
        reason = f"the module never recorded: {', '.join(missing)}"
    return {
        "name": check.name,
        "screen_id": check.screen_id,
        "passed": ui_passed and not missing,
        "ui_passed": ui_passed,
        "events": seen,
        "missing_events": missing,
        "message": "" if ui_passed and not missing else reason,
    }


def _mock_environment(stack: ExitStack, checks: list[FunctionalCheck]) -> dict[str, str]:
    environment: dict[str, str] = {}
    for check in checks:
        if check.mock:
            factory = MOCKS.get(check.mock)
            if factory is None:
                raise KeyError(f"functional check {check.name!r} needs unknown mock {check.mock!r}")
            environment.update(stack.enter_context(factory(check)))
        environment.update(check.env)
    return environment


def run(
    paths: RunPaths,
    *,
    udid: str,
    spec: dict[str, Any] | None = None,
    timeout: int = 1800,
) -> dict[str, Any]:
    """Run the app's functional checks on simulator ``udid``; write and return the report."""
    from iosforge.mvp.swiftui_gen import identity_from_project
    from iosforge.mvp.swiftui_scaffold import target_name

    data = spec if spec is not None else json.loads(paths.app_spec_json.read_text())
    checks = app_checks(paths, data)
    if not checks:
        report: dict[str, Any] = {"ok": True, "checks": [], "skipped": "no functional checks"}
        _write(paths, report)
        return report
    simulator.require_toolchain()
    identity = identity_from_project(paths.xcode_app)
    xcode.generate_project(paths.xcode_app)
    with ExitStack() as stack:
        environment = _mock_environment(stack, checks)
        clear_journal(udid, identity.bundle_id)
        outcome = xcode.run_ui_tests(
            paths.xcode_app,
            target_name(identity.app_name),
            udid=udid,
            derived_data=paths.run_dir / "DerivedDataFunctional",
            environment=environment,
            only=[f"{UI_TEST_TARGET}/FunctionalTests/{c.test_name}" for c in checks],
            timeout=timeout,
        )
    events = read_journal(udid, identity.bundle_id)
    results = [verdict(check, outcome, events) for check in checks]
    report = {"ok": all(r["passed"] for r in results), "checks": results}
    (paths.run_dir / "functional_tests.log").write_text(outcome.log[-200_000:], encoding="utf-8")
    _write(paths, report)
    log.info("functional.done", ok=report["ok"], checks=[(r["name"], r["passed"]) for r in results])
    return report


def failing_screens(report: dict[str, Any]) -> list[dict[str, str]]:
    """``{id, capability, message}`` for every failed check (corrective task input)."""
    return [
        {"id": str(r["screen_id"]), "capability": str(r["name"]), "message": str(r["message"])}
        for r in report.get("checks", [])
        if not r.get("passed")
    ]


def _write(paths: RunPaths, report: dict[str, Any]) -> None:
    (paths.run_dir / REPORT_NAME).write_text(
        json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8"
    )
