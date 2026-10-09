"""Permission-prompt registry and lint for generated SwiftUI apps."""

from __future__ import annotations

from pathlib import Path

import pytest

from iosforge.mvp.swiftui_permissions import (
    PERMISSION_KINDS,
    kind_for,
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
    assert all(kind.patterns for kind in PERMISSION_KINDS)


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
def test_kind_for_maps_spec_labels(label: str, case: str) -> None:
    kind = kind_for(label)
    assert kind is not None
    assert kind.case == case


@pytest.mark.parametrize("label", ["Battery level", "Dynamic island", "Haptics"])
def test_kind_for_ignores_unrelated_labels(label: str) -> None:
    assert kind_for(label) is None


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
    assert any(f" {case} permission API" in v for v in violations)


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
