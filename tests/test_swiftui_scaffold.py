"""Deterministic SwiftUI scaffold: screen-id contract wiring and XcodeGen project."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from iosforge.mvp.swiftui_permissions import PERMISSION_KINDS
from iosforge.mvp.swiftui_scaffold import (
    PENDING_MARKER,
    home_entry,
    pending_screens,
    screen_entries,
    target_name,
    write_scaffold,
)


def _spec(**extra: Any) -> dict[str, Any]:
    spec: dict[str, Any] = {
        "app_name": "Speaker & Headphone Test",
        "screens": [
            {"id": "0000", "name": "Splash / Launch"},
            {"id": "0001", "name": "Paywall - Pro Audio Tools"},
            {"id": "0011", "name": "Home - Test Hub", "route": "/"},
            {"id": "0013", "name": "Sound-Level Meter"},
        ],
        "permissions": [
            {"permission": "Microphone", "reason": "Measures sound level (inferred). Extra."},
        ],
    }
    spec.update(extra)
    return spec


def test_screen_entries_names_and_presentation() -> None:
    entries = {e.screen_id: e for e in screen_entries(_spec())}
    assert entries["0011"].case_name == "s0011"
    assert entries["0011"].type_name == "Screen0011View"
    assert entries["0011"].view_path == "App/Features/0011/Screen0011View.swift"
    assert entries["0000"].onboarding and entries["0000"].presentation == "root"
    assert entries["0001"].presentation == "sheet"
    assert entries["0011"].presentation == "root"
    assert entries["0013"].presentation == "push"


def test_declared_presentation_wins() -> None:
    spec = _spec(screens=[{"id": "x1", "name": "Detail", "presentation": "fullScreenCover"}])
    assert screen_entries(spec)[0].presentation == "fullScreenCover"


def test_home_entry_skips_onboarding() -> None:
    assert home_entry(screen_entries(_spec())).screen_id == "0011"


def test_target_name_is_an_identifier() -> None:
    assert target_name("Speaker & Headphone Test") == "SpeakerHeadphoneTest"
    assert target_name("2048 Puzzle") == "App2048Puzzle"
    assert target_name("!!!") == "GeneratedApp"


def test_write_scaffold_renders_contract_files(tmp_path: Path) -> None:
    fonts = tmp_path / "fonts"
    fonts.mkdir()
    (fonts / "Poppins-Bold.ttf").write_bytes(b"ttf")
    media = tmp_path / "media"
    media.mkdir()
    (media / "abc.png").write_bytes(b"png")
    app = tmp_path / "xcode_app"

    entries = write_scaffold(
        app,
        _spec(),
        app_name="Speaker & Headphone Test",
        bundle_id="com.example.speaker",
        fonts_dir=fonts,
        media_dir=media,
    )

    assert [e.screen_id for e in entries] == ["0000", "0001", "0011", "0013"]
    screen_id = (app / "App/Navigation/ScreenID.swift").read_text()
    assert 'case s0011 = "0011"' in screen_id
    assert "static let home: ScreenID = .s0011" in screen_id
    assert "static let onboardingStart: ScreenID? = .s0000" in screen_id
    assert "case .s0001: .sheet" in screen_id
    router = (app / "App/Navigation/Router.swift").read_text()
    assert "Headless.screenID" in router and "unknownID = raw" in router
    headless = (app / "App/Headless/Headless.swift").read_text()
    assert 'string(forKey: "screen-id")' in headless
    permissions = (app / "App/Permissions/Permissions.swift").read_text()
    assert "guard !Headless.isActive else { return false }" in permissions
    assert all(f"case {kind.case}" in permissions for kind in PERMISSION_KINDS)
    root = (app / "App/Navigation/RootView.swift").read_text()
    assert "UnknownScreenView" in root and "iosforge.unknown-screen" in root
    assert "struct SpeakerHeadphoneTestApp: App" in (app / "App/App.swift").read_text()

    project = (app / "project.yml").read_text()
    assert "PRODUCT_BUNDLE_IDENTIFIER: com.example.speaker" in project
    assert 'iOS: "17.0"' in project
    assert '- "Poppins-Bold.ttf"' in project
    assert 'NSMicrophoneUsageDescription: "Measures sound level."' in project
    assert "type: folder" in project
    assert (app / "Resources/Fonts/Poppins-Bold.ttf").exists()
    assert (app / "Resources/Media/abc.png").exists()
    assert pending_screens(app, entries) == ["0000", "0001", "0011", "0013"]


def test_rescaffold_keeps_model_owned_files(tmp_path: Path) -> None:
    app = tmp_path / "xcode_app"
    entries = write_scaffold(app, _spec(), app_name="A", bundle_id="com.example.a")
    view = app / "App/Features/0011/Screen0011View.swift"
    view.write_text('struct Screen0011View: View { var body: some View { Text("Hi") } }\n')
    router = app / "App/Navigation/Router.swift"
    router.write_text("tampered")

    write_scaffold(app, _spec(), app_name="A", bundle_id="com.example.a")

    assert 'Text("Hi")' in view.read_text()
    assert "func open(_ raw: String)" in router.read_text()
    assert "0011" not in pending_screens(app, entries)
    assert PENDING_MARKER in (app / "App/Features/0013/Screen0013View.swift").read_text()


def test_no_onboarding_screens_renders_false(tmp_path: Path) -> None:
    app = tmp_path / "xcode_app"
    write_scaffold(
        app, _spec(screens=[{"id": "0011", "name": "Home"}]), app_name="A", bundle_id="b.c"
    )
    screen_id = (app / "App/Navigation/ScreenID.swift").read_text()
    assert "static let onboardingStart: ScreenID? = nil" in screen_id
    assert "        false\n" in screen_id


def test_spec_without_screens_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="no screens"):
        write_scaffold(tmp_path, {"screens": []}, app_name="A", bundle_id="b.c")
