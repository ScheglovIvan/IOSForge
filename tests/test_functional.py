"""Level 2 Phase B: functional verification of capability modules (Linux logic + Mac e2e)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from iosforge.mvp import capability_registry, compliance, functional, swiftui_gen, xcode
from iosforge.mvp import swiftui_capabilities as caps
from iosforge.mvp import swiftui_functional as swf
from iosforge.mvp.paths import RunPaths
from iosforge.mvp.swiftui_prompts import _corrective_task_text
from iosforge.mvp.swiftui_scaffold import build_plan
from tests.test_compliance_ios import _first_iphone
from tests.test_feasibility import _spec_three_screens

ECHO_SWIFT = """import Foundation

/// Test capability module: echoes text and records the effect.
enum Echo {
    static func say(_ text: String) -> String {
        Functional.record("echo", ["text": text])
        return "ECHO:" + text.uppercased()
    }
}
"""

REAL_SCREEN = """import SwiftUI

struct Screen0001View: View {
    @State private var text = ""
    @State private var reply = ""

    var body: some View {
        VStack(spacing: 16) {
            TextField("Message", text: $text)
                .textFieldStyle(.roundedBorder)
            Button("Send") { reply = Echo.say(text) }
            Text(reply)
        }
        .padding()
    }
}
"""

SHELL_SCREEN = REAL_SCREEN.replace("reply = Echo.say(text)", 'reply = "ECHO:" + text.uppercased()')


def _echo_check(ctx: caps.CapabilityContext) -> swf.FunctionalCheck:
    return swf.FunctionalCheck(
        name="echo",
        screen_id=caps.capability_screen(ctx),
        steps=(
            swf.Step("type", "hello"),
            swf.Step("tap", "^send$"),
            swf.Step("wait_text", "ECHO:HELLO"),
        ),
        expect_events=("echo",),
    )


@pytest.fixture
def echo_module(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(
        capability_registry.MODULES,
        "echo_test",
        capability_registry.ModuleInfo("echo_test", ("other",), "test echo module"),
    )
    monkeypatch.setitem(
        caps.REGISTRY,
        "echo_test",
        caps.CapabilityDescriptor(
            key="echo_test",
            directory=caps.CAPABILITIES_DIR,
            render=lambda ctx: {f"{caps.CAPABILITIES_DIR}/Echo.swift": ECHO_SWIFT},
            screen_api_rule="Echo only through `Echo.say(_:)`.",
            functional_check=_echo_check,
        ),
    )


def _echo_spec() -> dict[str, Any]:
    spec = _spec_three_screens()
    spec["capabilities"] = [
        {
            "name": "echo",
            "kind": "other",
            "expected_behavior": "the echoed text is shown",
            "screens": ["0001"],
            "tier": 2,
            "module": "echo_test",
        }
    ]
    return spec


def _app(tmp_path: Path, spec: dict[str, Any]) -> RunPaths:
    paths = RunPaths.create(tmp_path / "runs")
    paths.app_spec_json.write_text(json.dumps(spec))
    swiftui_gen.write_scaffold(
        paths.xcode_app, spec, app_name="Echo Demo", bundle_id="dev.iosforge.echo"
    )
    return paths


def test_scaffold_without_checks_has_no_functional_layer(tmp_path: Path) -> None:
    paths = _app(tmp_path, _spec_three_screens())
    project = (paths.xcode_app / "project.yml").read_text()
    assert swf.UI_TEST_TARGET not in project and "    scheme: {}" in project
    assert not (paths.xcode_app / swf.UI_TESTS_FILE).exists()
    assert not (paths.xcode_app / swf.FUNCTIONAL_SWIFT).exists()


def test_scaffold_with_a_check_renders_the_functional_layer(
    tmp_path: Path, echo_module: None
) -> None:
    paths = _app(tmp_path, _echo_spec())
    project = (paths.xcode_app / "project.yml").read_text()
    assert f"      testTargets: [{swf.UI_TEST_TARGET}]" in project
    assert f"  {swf.UI_TEST_TARGET}:\n    type: bundle.ui-testing" in project
    tests = (paths.xcode_app / swf.UI_TESTS_FILE).read_text()
    assert "func test_echo() throws" in tests
    assert 'app.open(URL(string: "iosforge://screen/0001")!)' in tests
    assert 'try waitForText(app, "ECHO:HELLO", timeout: 10.0)' in tests
    assert "enum Functional" in (paths.xcode_app / swf.FUNCTIONAL_SWIFT).read_text()
    assert [c.name for c in functional.app_checks(paths, _echo_spec())] == ["echo"]


def test_launch_screens_are_skipped_by_the_driver() -> None:
    check = _echo_check(
        caps.CapabilityContext({}, caps.integ.Integrations(), {"screens": ["0001"]})
    )
    tests = swf.render_ui_tests([check], ["0000"])
    assert '"-iosforge.launch_shown.0000", "YES"' in tests


def test_parse_ui_tests() -> None:
    output = (
        "Test Case '-[FunctionalUITests.FunctionalTests test_echo]' started.\n"
        "/x/FunctionalTests.swift:30: error: -[FunctionalUITests.FunctionalTests test_ask] : "
        "XCTAssertTrue failed - text 'ANSWER' never appeared\n"
        "Test Case '-[FunctionalUITests.FunctionalTests test_echo]' passed (4.1 seconds).\n"
        "Test Case '-[FunctionalUITests.FunctionalTests test_ask]' failed (9.0 seconds).\n"
    )
    passed, failed = xcode.parse_ui_tests(output)
    assert passed == ["test_echo"]
    assert failed == {"test_ask": "XCTAssertTrue failed - text 'ANSWER' never appeared"}


@pytest.mark.parametrize(
    ("passed", "events", "ok", "message"),
    [
        (["test_echo"], [{"event": "echo", "check": "echo"}], True, ""),
        (["test_echo"], [], False, "the module never recorded: echo"),
        (["test_echo"], [{"event": "echo", "check": "other"}], False, "never recorded"),
        ([], [{"event": "echo", "check": "echo"}], False, "the UI test did not run"),
    ],
)
def test_verdict_needs_the_flow_and_the_module_effect(
    passed: list[str], events: list[dict[str, str]], ok: bool, message: str
) -> None:
    check = _echo_check(
        caps.CapabilityContext({}, caps.integ.Integrations(), {"screens": ["0001"]})
    )
    outcome = xcode.UITestOutcome(ok=bool(passed), passed=passed, failed={}, log="")
    result = functional.verdict(check, outcome, events)
    assert result["passed"] is ok and message in result["message"]


def test_failed_check_becomes_a_corrective_task_and_holds_the_gate() -> None:
    report = {
        "threshold": 0.8,
        "screens": [],
        "structural": {
            "ok": False,
            "functional_failures": [
                {"id": "0001", "capability": "echo", "message": "never recorded"}
            ],
        },
    }
    tasks = compliance.diffs_to_tasks(report, 2)["tasks"]
    assert tasks[0]["type"] == "fix_functional" and tasks[0]["screens"] == ["0001"]
    entry = build_plan(_spec_three_screens()).entries[1]
    text = _corrective_task_text(tasks[0], entry)
    assert "does NOT WORK" in text and "never recorded" in text
    from iosforge.worker.swiftui_build import hold_reason

    reason = hold_reason(
        {"status": "pass", "structural": report["structural"]}, structural_gate=True
    )
    assert reason is not None and "functional failures 1" in reason


def test_refine_folds_the_functional_report_into_the_audit(
    tmp_path: Path, echo_module: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _app(tmp_path, _echo_spec())
    seen: dict[str, Any] = {}
    monkeypatch.setattr(compliance, "build_ios", lambda p, timeout=0: Path("/tmp/App.app"))
    monkeypatch.setattr(compliance, "render_generated_ios", lambda p, env, app: None)
    monkeypatch.setattr(compliance, "nav_audit_ios", lambda p: {"ok": True, "missing_screens": []})
    monkeypatch.setattr(compliance, "blank_screens", lambda p, max_bytes: [])
    paths.generated_screens_json.write_text(json.dumps({"screens": []}))
    failing = {
        "ok": False,
        "checks": [{"name": "echo", "screen_id": "0001", "passed": False, "message": "x"}],
    }
    monkeypatch.setattr(functional, "run", lambda p, udid, spec: failing)

    def fake_refine(p: RunPaths, prepare: Any, **kw: Any) -> dict[str, Any]:
        prepare()
        seen["audit"] = kw["audit"]()
        return {}

    monkeypatch.setattr(compliance, "_refine", fake_refine)
    compliance.refine_ios_until_complete(
        paths,
        compliance.simulator.SimEnvironment(udid="U", locale="en-US"),
        threshold=0.8, soft_floor=0.6, max_iterations=1,
        weights=compliance.ComplianceWeights(0.4, 0.3, 0.1, 0.2), blank_max_bytes=6000,
    )  # fmt: skip
    audit = seen["audit"]
    assert audit["ok"] is False and audit["functional_failures"][0]["capability"] == "echo"


def _run_on_simulator(tmp_path: Path, screen: str) -> dict[str, Any]:
    udid = _first_iphone()
    assert udid is not None
    spec = _echo_spec()
    paths = _app(tmp_path, spec)
    (paths.xcode_app / "App/Features/0001/Screen0001View.swift").write_text(screen)
    return functional.run(paths, udid=udid, spec=spec, timeout=1200)


@pytest.mark.mac
@pytest.mark.skipif(_first_iphone() is None, reason="needs Xcode, XcodeGen and an iPhone simulator")
def test_working_capability_passes_on_the_simulator(tmp_path: Path, echo_module: None) -> None:
    report = _run_on_simulator(tmp_path, REAL_SCREEN)
    assert report["ok"], report
    assert report["checks"][0]["events"] == ["echo"]


@pytest.mark.mac
@pytest.mark.skipif(_first_iphone() is None, reason="needs Xcode, XcodeGen and an iPhone simulator")
def test_shell_capability_fails_on_the_simulator(tmp_path: Path, echo_module: None) -> None:
    report = _run_on_simulator(tmp_path, SHELL_SCREEN)
    check = report["checks"][0]
    assert not report["ok"] and check["ui_passed"] and check["missing_events"] == ["echo"]
