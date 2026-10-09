"""Functional verification contract of a generated app (Level 2 Phase B).

Headless screen-id rendering (what the Vision Judge sees) deliberately switches
behaviour off, so it cannot tell a working capability from a shell. Functional
verification runs the app LIVE instead:

* ``App/Capabilities/Functional.swift`` — functional mode (``-iosforge-functional``
  launch argument). Capability modules read their mock endpoints from ``IOSFORGE_*``
  launch environment values and record every observable effect to
  ``Documents/iosforge_functional.jsonl`` (tagged with the running check). The module
  is contract code, so its journal is trusted evidence that a screen really drove it.
* ``UITests/FunctionalTests.swift`` — one XCUITest per :class:`FunctionalCheck`: launch
  in functional mode with onboarding and launch screens skipped, open the capability
  screen through the ``iosforge://screen/<id>`` deep link, run the declarative steps
  (type into the first field, tap the action button, wait for a text) and fail when the
  expected on-screen text never appears.

:mod:`iosforge.mvp.functional` starts the mocks, runs the tests and checks the journal.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass, field

from iosforge.mvp.swiftui_templates import DO_NOT_EDIT

FUNCTIONAL_SWIFT = "App/Capabilities/Functional.swift"
UI_TESTS_DIR = "UITests"
UI_TESTS_FILE = f"{UI_TESTS_DIR}/FunctionalTests.swift"
UI_TEST_TARGET = "FunctionalUITests"
JOURNAL_NAME = "iosforge_functional.jsonl"
LAUNCH_FLAG = "-iosforge-functional"


@dataclass(frozen=True)
class Step:
    """One declarative UI step of a functional check.

    ``kind``: ``type`` (type ``value`` into the first text field / text view),
    ``tap`` (tap the hittable button whose whole label matches the ``value`` regex,
    else the shortest label containing a match; case-insensitive), ``wait_text`` (a static text containing ``value`` appears within
    ``timeout`` seconds) or ``pause`` (sleep ``timeout`` seconds).
    """

    kind: str
    value: str = ""
    timeout: float = 10.0


@dataclass(frozen=True)
class FunctionalCheck:
    """What proves a capability works: the flow on its screen and the expected effect."""

    name: str
    screen_id: str
    steps: tuple[Step, ...]
    expect_events: tuple[str, ...]
    mock: str = ""
    env: dict[str, str] = field(default_factory=dict)

    @property
    def test_name(self) -> str:
        return "test_" + re.sub(r"[^A-Za-z0-9_]", "_", self.name)


MockFactory = Callable[[FunctionalCheck], AbstractContextManager[dict[str, str]]]

#: Mock name → context manager yielding the ``IOSFORGE_*`` settings the app needs.
MOCKS: dict[str, MockFactory] = {}


def register_mock(name: str, factory: MockFactory) -> MockFactory:
    """Make a mock available to the functional checks that name it."""
    MOCKS[name] = factory
    return factory


FUNCTIONAL = f"""import Foundation

/// Functional verification mode (`{LAUNCH_FLAG}` launch argument). The app runs live,
/// capability modules talk to the mocks named by `IOSFORGE_*` launch environment values
/// and record their observable effects to `Documents/{JOURNAL_NAME}`. {DO_NOT_EDIT}
enum Functional {{
    static let isActive = ProcessInfo.processInfo.arguments.contains("{LAUNCH_FLAG}")

    /// A mock setting (`IOSFORGE_<key>`), only in functional mode.
    static func value(_ key: String) -> String? {{
        guard isActive else {{ return nil }}
        return ProcessInfo.processInfo.environment["IOSFORGE_\\(key)"]
    }}

    /// Appends one effect to the journal, tagged with the running check.
    static func record(_ event: String, _ fields: [String: String] = [:]) {{
        guard isActive,
              let folder = FileManager.default.urls(for: .documentDirectory, in: .userDomainMask).first
        else {{ return }}
        var entry = fields
        entry["event"] = event
        entry["check"] = ProcessInfo.processInfo.environment["IOSFORGE_CHECK"] ?? ""
        guard let data = try? JSONSerialization.data(withJSONObject: entry, options: [.sortedKeys])
        else {{ return }}
        let line = data + Data([0x0A])
        let url = folder.appendingPathComponent("{JOURNAL_NAME}")
        if let handle = try? FileHandle(forWritingTo: url) {{
            handle.seekToEndOfFile()
            handle.write(line)
            try? handle.close()
        }} else {{
            try? line.write(to: url)
        }}
    }}
}}
"""


_ESCAPES = {"\\": "\\\\", '"': '\\"', "\n": "\\n", "\r": "\\r", "\t": "\\t"}


def swift_literal(text: str) -> str:
    """``text`` as a Swift string literal (only escapes Swift accepts)."""
    body = "".join(_ESCAPES.get(ch, f"\\u{{{ord(ch):x}}}" if ord(ch) < 0x20 else ch) for ch in text)
    return f'"{body}"'


_swift = swift_literal


def _step(step: Step) -> str:
    if step.kind == "type":
        return f"        typeIntoFirstField(app, {_swift(step.value)})"
    if step.kind == "tap":
        return (
            f"        try tapAction(app, matching: {_swift(step.value)}, timeout: {step.timeout})"
        )
    if step.kind == "wait_text":
        return f"        try waitForText(app, {_swift(step.value)}, timeout: {step.timeout})"
    if step.kind == "pause":
        return f"        sleep({max(1, int(step.timeout))})"
    raise ValueError(f"unknown functional step {step.kind!r}")


def _test(check: FunctionalCheck, launch_keys: list[str]) -> str:
    skips = "".join(f', "-iosforge.launch_shown.{key}", "YES"' for key in launch_keys)
    steps = "\n".join(_step(step) for step in check.steps)
    return f"""    func {check.test_name}() throws {{
        let app = XCUIApplication()
        app.launchArguments = ["{LAUNCH_FLAG}", "-iosforge.onboarding_done", "YES"{skips}]
        app.launchEnvironment = forwardedEnvironment(check: {_swift(check.name)})
        app.launch()
        _ = app.wait(for: .runningForeground, timeout: 15)
        app.open(URL(string: "iosforge://screen/{check.screen_id}")!)
        _ = app.wait(for: .runningForeground, timeout: 15)
        let screen = app.descendants(matching: .any)["iosforge.screen.{check.screen_id}"].firstMatch
        XCTAssertTrue(screen.waitForExistence(timeout: 15), "screen {check.screen_id} never opened")
        sleep(1)
{steps}
    }}"""


def render_ui_tests(checks: list[FunctionalCheck], launch_screen_ids: list[str]) -> str:
    """``UITests/FunctionalTests.swift``: one XCUITest per functional check."""
    names = [check.test_name for check in checks]
    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        raise ValueError(f"functional checks collide on test names: {duplicates}")
    tests = "\n\n".join(_test(check, launch_screen_ids) for check in checks)
    return f"""import XCTest

/// Functional checks of the app's capability modules (run against their mocks).
/// {DO_NOT_EDIT}
final class FunctionalTests: XCTestCase {{
    override func setUp() {{
        continueAfterFailure = false
    }}

{tests}

    private func forwardedEnvironment(check: String) -> [String: String] {{
        var environment = ProcessInfo.processInfo.environment.filter {{ $0.key.hasPrefix("IOSFORGE_") }}
        environment["IOSFORGE_CHECK"] = check
        return environment
    }}

    private func typeIntoFirstField(_ app: XCUIApplication, _ text: String) {{
        let deadline = Date().addingTimeInterval(8)
        while Date() < deadline {{
            for field in [app.textFields.firstMatch, app.textViews.firstMatch, app.searchFields.firstMatch]
            where field.exists && field.isHittable {{
                field.tap()
                field.typeText(text)
                return
            }}
            sleep(1)
        }}
        XCTFail("no text field on screen to type '\\(text)' into")
    }}

    private func tapAction(_ app: XCUIApplication, matching pattern: String, timeout: TimeInterval) throws {{
        let exact = try NSRegularExpression(pattern: "^(?:" + pattern + ")$", options: [.caseInsensitive])
        let loose = try NSRegularExpression(pattern: pattern, options: [.caseInsensitive])
        let deadline = Date().addingTimeInterval(timeout)
        while Date() < deadline {{
            let buttons = app.buttons.allElementsBoundByIndex.filter {{ $0.exists && $0.isHittable }}
            func matches(_ regex: NSRegularExpression, _ label: String) -> Bool {{
                regex.firstMatch(in: label, range: NSRange(label.startIndex..., in: label)) != nil
            }}
            if let button = buttons.first(where: {{ matches(exact, $0.label.trimmingCharacters(in: .whitespaces)) }}) {{
                button.tap()
                return
            }}
            let loosely = buttons.filter {{ matches(loose, $0.label) }}
            if let button = loosely.min(by: {{ $0.label.count < $1.label.count }}) {{
                button.tap()
                return
            }}
            sleep(1)
        }}
        XCTFail("no button matching /\\(pattern)/ on screen")
    }}

    private func waitForText(_ app: XCUIApplication, _ text: String, timeout: TimeInterval) throws {{
        let predicate = NSPredicate(format: "label CONTAINS %@", text)
        let found = app.staticTexts.containing(predicate).firstMatch.waitForExistence(timeout: timeout)
            || app.otherElements.containing(predicate).firstMatch.exists
        XCTAssertTrue(found, "text '\\(text)' never appeared")
    }}
}}
"""


def render_files(checks: list[FunctionalCheck], launch_screen_ids: list[str]) -> dict[str, str]:
    """Contract files of functional verification (none when the app has no checks)."""
    if not checks:
        return {}
    return {
        FUNCTIONAL_SWIFT: FUNCTIONAL,
        UI_TESTS_FILE: render_ui_tests(checks, launch_screen_ids),
    }


def project_target(target: str, bundle_id: str) -> list[str]:
    """XcodeGen lines of the UI test target that runs the functional checks."""
    return [
        f"  {UI_TEST_TARGET}:",
        "    type: bundle.ui-testing",
        "    platform: iOS",
        f"    sources: [{UI_TESTS_DIR}]",
        "    dependencies:",
        f"      - target: {target}",
        "    settings:",
        "      base:",
        "        GENERATE_INFOPLIST_FILE: YES",
        "        CODE_SIGNING_ALLOWED: NO",
        f"        PRODUCT_BUNDLE_IDENTIFIER: {bundle_id}.functional",
        '        SWIFT_VERSION: "5.0"',
    ]
