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
from contextlib import ExitStack
from typing import Any

from iosforge.common.logging import get_logger
from iosforge.mvp import simulator, xcode
from iosforge.mvp import swiftui_capabilities as caps
from iosforge.mvp import swiftui_functional as swf
from iosforge.mvp import swiftui_integrations as integ
from iosforge.mvp.paths import RunPaths
from iosforge.mvp.swiftui_functional import JOURNAL_NAME, UI_TEST_TARGET, FunctionalCheck

log = get_logger("mvp.functional")

REPORT_NAME = "functional_report.json"

MOCKS = swf.MOCKS
register_mock = swf.register_mock


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


def describe_flow(check: FunctionalCheck) -> str:
    """What the driver does on the screen, in words (for the corrective prompt)."""
    words = {
        "type": "types {value!r} into the first text field",
        "tap": "taps the button whose label is exactly /{value}/ (else the shortest label "
        "containing it, case-insensitive)",
        "wait_text": "waits up to {timeout:g}s for a text containing {value!r}",
        "pause": "waits {timeout:g}s",
    }
    steps = [
        words[s.kind].format(value=s.value, timeout=s.timeout)
        for s in check.steps
        if s.kind in words
    ]
    effects = ", ".join(check.expect_events) or "nothing"
    return f"opens screen {check.screen_id}, {'; '.join(steps)}; the module must record: {effects}"


def verdict(
    check: FunctionalCheck, outcome: xcode.UITestOutcome, events: list[dict[str, str]]
) -> dict[str, Any]:
    """One check's result: UI flow passed and every expected effect recorded (pure).

    A failure of the test infrastructure itself (xcodebuild timed out, the runner did not
    start, the test never ran) is an ``infra_error``, not evidence that the capability is
    broken.
    """
    seen = [e["event"] for e in events if e.get("check") == check.name]
    missing = [event for event in check.expect_events if event not in seen]
    ui_passed = check.test_name in outcome.passed
    test_failure = outcome.failed.get(check.test_name, "")
    infra = not ui_passed and not test_failure
    reason = test_failure
    if infra:
        reason = outcome.failed.get("xcodebuild") or "the UI test did not run"
    if ui_passed and missing:
        reason = f"the module never recorded: {', '.join(missing)}"
    return {
        "name": check.name,
        "screen_id": check.screen_id,
        "passed": ui_passed and not missing,
        "ui_passed": ui_passed,
        "infra_error": infra,
        "events": seen,
        "missing_events": missing,
        "flow": describe_flow(check),
        "message": "" if ui_passed and not missing else reason[-600:],
    }


def _merge(environment: dict[str, str], values: dict[str, str], source: str) -> None:
    for key, value in values.items():
        if not key.startswith("IOSFORGE_"):
            raise ValueError(f"{source}: mock setting {key!r} must start with IOSFORGE_")
        if key in environment and environment[key] != value:
            raise ValueError(f"{source}: mock setting {key!r} collides with another check")
        environment[key] = value


def mock_environment(stack: ExitStack, checks: list[FunctionalCheck]) -> dict[str, str]:
    """Start every check's mock and merge the ``IOSFORGE_*`` settings they yield."""
    environment: dict[str, str] = {}
    for check in checks:
        if check.mock:
            factory = MOCKS.get(check.mock)
            if factory is None:
                raise KeyError(f"functional check {check.name!r} needs unknown mock {check.mock!r}")
            _merge(environment, stack.enter_context(factory(check)), check.name)
        _merge(environment, check.env, check.name)
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
        environment = mock_environment(stack, checks)
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
    report = {
        "ok": all(r["passed"] for r in results),
        "infra_error": any(r["infra_error"] for r in results),
        "checks": results,
    }
    (paths.run_dir / "functional_tests.log").write_text(outcome.log[-200_000:], encoding="utf-8")
    _write(paths, report)
    log.info("functional.done", ok=report["ok"], checks=[(r["name"], r["passed"]) for r in results])
    return report


def run_safely(
    paths: RunPaths,
    *,
    udid: str,
    spec: dict[str, Any] | None = None,
    timeout: int = 1800,
) -> dict[str, Any]:
    """:func:`run`, retried once on an infrastructure error; never raises.

    A run that cannot even start (unknown mock, no toolchain, project generation) comes
    back as a failed report with ``infra_error`` so the caller holds the job instead of
    crashing or blaming the screens.
    """
    report: dict[str, Any] = {}
    for _ in range(2):
        try:
            report = run(paths, udid=udid, spec=spec, timeout=timeout)
        except (KeyError, ValueError) as exc:
            log.error("functional.config_error", error=str(exc))
            report = {"ok": False, "infra_error": True, "checks": [], "error": str(exc)[:600]}
            _write(paths, report)
            return report
        except Exception as exc:
            log.error("functional.crashed", error=str(exc))
            report = {"ok": False, "infra_error": True, "checks": [], "error": str(exc)[:600]}
            _write(paths, report)
        if not report.get("infra_error"):
            return report
    return report


def failing_screens(report: dict[str, Any]) -> list[dict[str, str]]:
    """``{id, capability, message, flow}`` for every check the capability itself failed.

    Infrastructure errors are excluded: they hold the gate (:func:`infra_errors`) but must
    not send a correct screen to a corrective rewrite.
    """
    return [
        {
            "id": str(r["screen_id"]),
            "capability": str(r["name"]),
            "message": str(r["message"]),
            "flow": str(r.get("flow", "")),
        }
        for r in report.get("checks", [])
        if not r.get("passed") and not r.get("infra_error")
    ]


def infra_errors(report: dict[str, Any]) -> list[dict[str, str]]:
    """Infrastructure failures of a functional run (they hold the gate, no corrective task)."""
    errors = [
        {"capability": str(r["name"]), "message": str(r["message"])}
        for r in report.get("checks", [])
        if r.get("infra_error")
    ]
    if report.get("error") and not errors:
        errors.append({"capability": "", "message": str(report["error"])})
    return errors


def _write(paths: RunPaths, report: dict[str, Any]) -> None:
    (paths.run_dir / REPORT_NAME).write_text(
        json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8"
    )
