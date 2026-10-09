"""Permission-prompt registry and lint for generated SwiftUI apps."""

from __future__ import annotations

from pathlib import Path

import pytest

from iosforge.mvp.swiftui_permissions import (
    PERMISSION_KINDS,
    blank_comments_and_strings,
    kinds_for,
    permission_violations,
)

REQUIRED_KINDS = {
    "notifications",
    "tracking",
    "location",
    "camera",
    "microphone",
    "photos",
    "contacts",
    "calendar",
    "reminders",
    "health",
    "motion",
    "speech",
    "bluetooth",
    "face_id",
    "media_library",
    "family_controls",
}


def _write(app: Path, rel: str, text: str) -> None:
    path = app / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def test_registry_covers_every_required_kind() -> None:
    cases = {kind.case for kind in PERMISSION_KINDS}
    assert REQUIRED_KINDS <= cases
    assert len(cases) == len(PERMISSION_KINDS)
    assert all(kind.patterns or kind.capture for kind in PERMISSION_KINDS)


@pytest.mark.parametrize(
    ("label", "case"),
    [
        ("Microphone", "microphone"),
        ("App Tracking Transparency (ATT)", "tracking"),
        ("Notifications", "notifications"),
        ("Photo Library", "photos"),
        ("HealthKit", "health"),
        ("Calendars", "calendar"),
        ("Screen Time (FamilyControls)", "family_controls"),
        ("Location (GPS)", "location"),
    ],
)
def test_kinds_for_maps_spec_labels(label: str, case: str) -> None:
    assert [kind.case for kind in kinds_for(label)] == [case]


@pytest.mark.parametrize(
    "label", ["Battery level", "Dynamic island", "Haptics", "Screen recording", "Home screen"]
)
def test_kinds_for_ignores_unrelated_labels(label: str) -> None:
    assert kinds_for(label) == []


def test_kinds_for_returns_every_mentioned_kind() -> None:
    assert [k.case for k in kinds_for("Camera and Photo Library")] == ["camera", "photos"]


@pytest.mark.parametrize(
    ("snippet", "case"),
    [
        (
            "UNUserNotificationCenter.current().requestAuthorization(options: [.alert])",
            "notifications",
        ),
        ("ATTrackingManager.requestTrackingAuthorization { _ in }", "tracking"),
        ("manager.requestWhenInUseAuthorization()", "location"),
        ("await AVCaptureDevice.requestAccess(for: .video)", "camera"),
        ("AVAudioApplication.requestRecordPermission { _ in }", "microphone"),
        ("PHPhotoLibrary.requestAuthorization(for: .readWrite) { _ in }", "photos"),
        ("try await CNContactStore().requestAccess(for: .contacts)", "contacts"),
        ("try await store.requestFullAccessToEvents()", "calendar"),
        ("try await store.requestFullAccessToReminders()", "reminders"),
        ("HKHealthStore().requestAuthorization(toShare: [], read: [])", "health"),
        ("let pedometer = CMPedometer()", "motion"),
        ("SFSpeechRecognizer.requestAuthorization { _ in }", "speech"),
        ("let central = CBCentralManager(delegate: nil, queue: nil)", "bluetooth"),
        (
            "context.evaluatePolicy(.deviceOwnerAuthentication, localizedReason: r) { _, _ in }",
            "face_id",
        ),
        (
            "try await AuthorizationCenter.shared.requestAuthorization(for: .individual)",
            "family_controls",
        ),
    ],
)
def test_prompt_api_outside_gate_is_a_violation(tmp_path: Path, snippet: str, case: str) -> None:
    _write(tmp_path, "App/Features/0001/Screen0001View.swift", f"func go() {{\n    {snippet}\n}}\n")
    violations = permission_violations(tmp_path)
    assert violations
    assert all("App/Features/0001/Screen0001View.swift:2: error:" in v for v in violations)
    assert any(f" {case} permission API" in v or f" {case} capture API" in v for v in violations)


def test_prompt_api_inside_permissions_dir_is_allowed(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "App/Permissions/MicrophonePermission.swift",
        "enum MicrophonePermission: PermissionPrompter {\n"
        "    static let kind: PermissionKind = .microphone\n"
        "    static func prompt() async -> Bool {\n"
        "        await AVAudioApplication.requestRecordPermission()\n"
        "    }\n}\n",
    )
    _write(
        tmp_path,
        "App/Features/0013/Screen0013View.swift",
        'Button("Start") { Task { await Permissions.request(MicrophonePermission.self) } }\n',
    )
    assert permission_violations(tmp_path) == []


def test_direct_prompt_call_bypassing_gate_is_a_violation(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "App/Features/0013/Screen0013View.swift",
        "_ = await MicrophonePermission.prompt()\n",
    )
    violations = permission_violations(tmp_path)
    assert len(violations) == 1
    assert "bypasses the headless gate" in violations[0]


def test_commented_out_calls_are_ignored(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "App/Features/0001/Screen0001View.swift",
        "// CBCentralManager(delegate: nil, queue: nil)\n/* CMPedometer()\n */\nlet x = 1\n",
    )
    assert permission_violations(tmp_path) == []


def test_generic_request_authorization_on_a_variable_is_caught(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "App/Features/0001/Screen0001View.swift",
        "let center = UNUserNotificationCenter.current()\n"
        + "\n" * 5
        + "center.requestAuthorization(options: [.alert]) { _, _ in }\n",
    )
    violations = permission_violations(tmp_path)
    assert len(violations) == 1 and ":7: error:" in violations[0]


def test_url_string_does_not_hide_following_code(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "App/Features/0001/Screen0001View.swift",
        'let url = URL(string: "https://example.com/a"); '
        "ATTrackingManager.requestTrackingAuthorization { _ in }\n",
    )
    assert any(" tracking permission API" in v for v in permission_violations(tmp_path))


def test_api_names_inside_strings_are_not_flagged(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "App/Features/0001/Screen0001View.swift",
        'Text("Tap to requestAuthorization() later")\nlet s = """\nCMPedometer()\n"""\n',
    )
    assert permission_violations(tmp_path) == []


def test_any_direct_prompt_call_is_flagged(tmp_path: Path) -> None:
    _write(
        tmp_path, "App/Features/0001/Screen0001View.swift", "_ = await CameraPrompter.prompt()\n"
    )
    assert "bypasses the headless gate" in permission_violations(tmp_path)[0]


def test_blanking_keeps_offsets_and_lines() -> None:
    source = 'a // x\n/* y\n /* nested */ z */ "s\\"q" b'
    blanked = blank_comments_and_strings(source)
    assert len(blanked) == len(source)
    assert blanked.count("\n") == source.count("\n")
    assert "x" not in blanked and "nested" not in blanked and "q" not in blanked
    assert blanked.rstrip().endswith("b")


def test_capture_api_allowed_in_guarded_service(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "App/Features/0013/LevelMeterService.swift",
        "final class LevelMeterService {\n"
        "    func start() {\n"
        "        guard !Headless.isActive else { return }\n"
        "        let input = engine.inputNode\n"
        "    }\n}\n",
    )
    assert permission_violations(tmp_path) == []


def test_capture_api_in_unguarded_service_is_flagged(tmp_path: Path) -> None:
    _write(tmp_path, "App/Features/0013/LevelMeterService.swift", "let input = engine.inputNode\n")
    violations = permission_violations(tmp_path)
    assert len(violations) == 1 and "guard it with `Headless.isActive`" in violations[0]


def test_capture_api_in_a_view_is_flagged(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "App/Features/0013/Screen0013View.swift",
        "if Headless.isActive { return }\nlet input = engine.inputNode\n",
    )
    violations = permission_violations(tmp_path)
    assert len(violations) == 1 and "move it into a `*Service.swift`" in violations[0]


def test_undeclared_prompter_is_flagged(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "App/Features/0013/Screen0013View.swift",
        "Task { await Permissions.request(CameraPermission.self) }\n"
        "Task { await Permissions.request(MicrophonePermission.self) }\n",
    )
    violations = permission_violations(tmp_path, declared={"MicrophonePermission"})
    assert len(violations) == 1
    assert "`CameraPermission` is not declared" in violations[0]
    assert "MicrophonePermission" in violations[0]


def test_one_error_per_call_site(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "App/Features/0001/Screen0001View.swift",
        "await AVCaptureDevice.requestAccess(for: AVMediaType.video)\n",
    )
    assert len(permission_violations(tmp_path)) == 1


def test_pasteboard_write_is_not_a_prompt(tmp_path: Path) -> None:
    _write(
        tmp_path, "App/Features/0001/Screen0001View.swift", 'UIPasteboard.general.string = "code"\n'
    )
    assert permission_violations(tmp_path) == []


def test_weak_headless_mention_is_not_a_guard(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "App/Features/0013/LevelMeterService.swift",
        "let _ = Headless.isActive\nlet input = engine.inputNode\n",
    )
    assert len(permission_violations(tmp_path)) == 1
