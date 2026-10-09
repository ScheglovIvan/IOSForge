"""Deterministic SwiftUI scaffold: navigation plan, contract files and strict enforcement."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from iosforge.mvp.swiftui_navigation import NavigationSpecError
from iosforge.mvp.swiftui_permissions import PERMISSION_KINDS
from iosforge.mvp.swiftui_prompters import PROMPTERS
from iosforge.mvp.swiftui_scaffold import (
    PENDING_MARKER,
    AppIdentity,
    build_plan,
    enforce_contract,
    pending_screens,
    target_name,
    write_scaffold,
)
from iosforge.mvp.swiftui_templates import swift_str


def _spec(**extra: Any) -> dict[str, Any]:
    spec: dict[str, Any] = {
        "app_name": "Speaker & Headphone Test",
        "screens": [
            {"id": "0000", "name": "Splash / Launch"},
            {"id": "0001", "name": "Paywall - Pro Audio Tools"},
            {"id": "0011", "name": "Home - Test Hub", "route": "/", "navigates_to": ["0013"]},
            {"id": "0013", "name": "Sound-Level Meter", "navigates_to": ["0011"]},
        ],
        "navigation": {"type": "stack", "map": [], "deep_links": []},
        "permissions": [
            {"permission": "Microphone", "reason": "Measures sound level (inferred). Extra."},
        ],
    }
    spec.update(extra)
    return spec


def _tabbed() -> dict[str, Any]:
    bar = "Cleaner{a} / Meter{b}"
    return _spec(
        screens=[
            {"id": "0000", "name": "Splash / Launch"},
            {"id": "0001", "name": "Paywall - Pro"},
            {
                "id": "0011",
                "name": "Home",
                "components": [{"type": "tab_bar", "data": bar.format(a=" (active)", b="")}],
                "navigates_to": ["0008"],
            },
            {
                "id": "0013",
                "name": "Meter",
                "components": [{"type": "tab_bar", "data": bar.format(a="", b=" (active)")}],
            },
            {"id": "0008", "name": "Instructions"},
        ],
        navigation={"type": "tab_bar_with_stack", "map": [], "deep_links": []},
    )


def test_plan_without_tabs_uses_one_hidden_tab() -> None:
    plan = build_plan(_spec())
    entries = {e.screen_id: e for e in plan.entries}
    assert not plan.shows_tab_bar
    assert [t.root.screen_id for t in plan.tabs] == ["0011"]
    assert entries["0000"].presentation == "onboarding" and entries["0000"].tab_root is None
    assert entries["0001"].presentation == "fullScreenCover" and entries["0001"].tab_root is None
    assert entries["0011"].presentation == "tabRoot" and entries["0011"].tab_root == "0011"
    assert entries["0013"].presentation == "push" and entries["0013"].tab_root == "0011"
    assert entries["0011"].fixtures_path == "App/Fixtures/Fixtures0011.swift"


def test_plan_with_tabs() -> None:
    plan = build_plan(_tabbed())
    assert plan.shows_tab_bar
    assert [(t.case_name, t.title, t.root.screen_id) for t in plan.tabs] == [
        ("t0011", "Cleaner", "0011"),
        ("t0013", "Meter", "0013"),
    ]
    entries = {e.screen_id: e for e in plan.entries}
    assert entries["0013"].presentation == "tabRoot"
    assert entries["0008"].tab_root == "0011"
    assert entries["0011"].shows_tab_bar and entries["0013"].shows_tab_bar
    assert not entries["0008"].shows_tab_bar and not entries["0001"].shows_tab_bar


def test_unreachable_screen_fails_the_plan() -> None:
    spec = _tabbed()
    spec["screens"].append({"id": "0009", "name": "Running"})
    with pytest.raises(NavigationSpecError, match="0009"):
        build_plan(spec)


def test_case_names_are_safe_and_unique() -> None:
    spec = _spec(
        screens=[
            {"id": "a-b", "route": "/", "navigates_to": ["a_b", "default", "0011"]},
            {"id": "a_b"},
            {"id": "default"},
            {"id": "0011", "name": "x\ny"},
        ]
    )
    entries = build_plan(spec).entries
    assert [e.case_name for e in entries] == ["sA_b", "sA_b_2", "sDefault", "s0011"]
    assert len({e.type_name for e in entries}) == 4
    assert entries[3].name == "x y"


def test_duplicate_screen_ids_are_rejected() -> None:
    with pytest.raises(ValueError, match="duplicate screen id"):
        build_plan(_spec(screens=[{"id": "1"}, {"id": "1"}]))


def test_target_name_is_an_identifier() -> None:
    assert target_name("Speaker & Headphone Test") == "SpeakerHeadphoneTest"
    assert target_name("2048 Puzzle") == "App2048Puzzle"
    assert target_name("!!!") == "GeneratedApp"


def test_swift_string_escaping() -> None:
    assert swift_str('a"b\\c\nd\x01é') == '"a\\"b\\\\c\\nd\\u{1}é"'


def test_write_scaffold_renders_contract_files(tmp_path: Path) -> None:
    fonts = tmp_path / "fonts"
    fonts.mkdir()
    (fonts / "Poppins-Bold.ttf").write_bytes(b"ttf")
    media = tmp_path / "media"
    media.mkdir()
    (media / "abc.png").write_bytes(b"png")
    app = tmp_path / "xcode_app"

    entries = write_scaffold(
        app, _tabbed(), app_name="Speaker & Headphone Test", bundle_id="com.example.s",
        fonts_dir=fonts, media_dir=media,
    )  # fmt: skip

    screen_id = (app / "App/Navigation/ScreenID.swift").read_text()
    assert 'case s0011 = "0011"' in screen_id
    assert "case .s0001: .fullScreenCover" in screen_id and "case .s0008: .t0011" in screen_id
    assert "[.s0011, .s0013].contains(self)" in screen_id
    assert "static let onboardingStart: ScreenID? = .s0000" in screen_id
    app_tab = (app / "App/Navigation/AppTab.swift").read_text()
    assert 'case t0013 = "0013"' in app_tab and "static let showsTabBar = true" in app_tab
    router = (app / "App/Navigation/Router.swift").read_text()
    assert "Headless.screenID" in router and "unknownID = raw" in router
    root = (app / "App/Navigation/RootView.swift").read_text()
    assert "AppTabBar(tabs: AppTab.allCases, selection: $router.selectedTab)" in root
    gate = (app / "App/Permissions/Permissions.swift").read_text()
    assert "guard !Headless.isActive else { return false }" in gate
    assert all(f"case {kind.case}" in gate for kind in PERMISSION_KINDS)
    mic = (app / "App/Permissions/MicrophonePermission.swift").read_text()
    assert "AVAudioApplication.requestRecordPermission" in mic
    assert sorted(p.name for p in (app / "App/Permissions").iterdir()) == [
        "MicrophonePermission.swift",
        "Permissions.swift",
    ]
    assert (app / "App/Components/AppTabBar.swift").exists()

    project = (app / "project.yml").read_text()
    assert "PRODUCT_BUNDLE_IDENTIFIER: com.example.s" in project
    assert "path: Config/Info.plist" in project and "    scheme: {}" in project
    assert '- "Poppins-Bold.ttf"' in project and "type: folder" in project
    assert 'includes: ["*.ttf", "*.otf"]' in project
    assert 'NSMicrophoneUsageDescription: "Measures sound level."' in project
    assert pending_screens(app, entries) == ["0000", "0001", "0011", "0013", "0008"]


def test_every_permission_kind_has_a_prompter() -> None:
    assert {kind.case for kind in PERMISSION_KINDS} == set(PROMPTERS)


def test_multi_kind_permission_gets_every_purpose_string(tmp_path: Path) -> None:
    spec = _spec(permissions=[{"permission": "Camera and Photo Library", "reason": "Scan docs."}])
    write_scaffold(tmp_path, spec, app_name="A", bundle_id="b.c")
    project = (tmp_path / "project.yml").read_text()
    assert "NSCameraUsageDescription" in project and "NSPhotoLibraryUsageDescription" in project
    assert (tmp_path / "App/Permissions/CameraPermission.swift").exists()
    assert (tmp_path / "App/Permissions/PhotosPermission.swift").exists()


def test_rescaffold_keeps_model_owned_files(tmp_path: Path) -> None:
    app = tmp_path / "xcode_app"
    entries = write_scaffold(app, _spec(), app_name="A", bundle_id="com.example.a")
    view = app / "App/Features/0011/Screen0011View.swift"
    view.write_text('struct Screen0011View: View { var body: some View { Text("Hi") } }\n')
    write_scaffold(app, _spec(), app_name="A", bundle_id="com.example.a")
    assert 'Text("Hi")' in view.read_text()
    assert "0011" not in pending_screens(app, entries)
    assert PENDING_MARKER in (app / "App/Features/0013/Screen0013View.swift").read_text()
    (app / "App/Features/0013/Screen0013View.swift").unlink()
    assert pending_screens(app, entries) == ["0000", "0001", "0013"]


def test_enforce_contract_is_strict(tmp_path: Path) -> None:
    app = tmp_path / "xcode_app"
    write_scaffold(app, _spec(), app_name="A", bundle_id="com.example.a")
    identity = AppIdentity("A", "com.example.a")
    assert enforce_contract(app, _spec(), identity).tampered == []

    gate = app / "App/Permissions/Permissions.swift"
    gate.write_text(gate.read_text().replace("guard !Headless.isActive else { return false }", ""))
    (app / "App/Headless/Headless.swift").unlink()
    (app / "App/Navigation/MyRouter.swift").write_text("struct MyRouter {}\n")
    (app / "App/Permissions/CameraPrompter.swift").write_text("enum CameraPrompter {}\n")
    (app / "App/Helpers.swift").write_text("let x = 1\n")
    (app / "App/Services").mkdir()
    (app / "App/Features/9999").mkdir()
    (app / "Package.swift").write_text("// swift-tools-version:5.9\n")
    (app / "Resources/Fonts").mkdir(parents=True)
    (app / "Resources/Fonts/Sneaky.swift").write_text("let sneaky = 1\n")

    report = enforce_contract(app, _spec(), identity)

    assert report.restored == ["App/Headless/Headless.swift", "App/Permissions/Permissions.swift"]
    assert sorted(report.removed) == [
        "App/Helpers.swift",
        "App/Navigation/MyRouter.swift",
        "App/Permissions/CameraPrompter.swift",
    ]
    assert report.unexpected == [
        "App/Services",
        "Package.swift",
        "Resources/Fonts/Sneaky.swift",
        "App/Features/9999",
    ]
    assert "guard !Headless.isActive" in gate.read_text()
    assert not (app / "App/Navigation/MyRouter.swift").exists()


def test_pushed_screen_with_a_tab_bar_component_keeps_the_bar() -> None:
    spec = _tabbed()
    spec["screens"][4]["components"] = [{"type": "tab_bar", "data": "Cleaner / Meter"}]
    assert {e.screen_id: e for e in build_plan(spec).entries}["0008"].shows_tab_bar


def test_pushed_screen_claiming_an_active_tab_is_ambiguous() -> None:
    spec = _tabbed()
    spec["screens"][4]["components"] = [{"type": "tab_bar", "data": "Cleaner (active) / Meter"}]
    with pytest.raises(NavigationSpecError, match="'Cleaner'"):
        build_plan(spec)


@pytest.mark.parametrize(
    ("name", "presentation"),
    [("Paywall - Pro", "fullScreenCover"), ("Upgrade to Premium", "fullScreenCover"),
     ("Share Sheet", "sheet"), ("Rating popup", "sheet"), ("Settings", "push")],
)  # fmt: skip
def test_presentation_heuristic(name: str, presentation: str) -> None:
    spec = _spec(
        screens=[
            {"id": "0011", "name": "Home", "route": "/", "navigates_to": ["x"]},
            {"id": "x", "name": name},
        ]
    )
    assert build_plan(spec).entries[1].presentation == presentation


def test_tab_root_with_an_onboarding_like_name_is_not_onboarding() -> None:
    spec = _tabbed()
    spec["screens"][3]["name"] = "Welcome Meter"
    entries = {e.screen_id: e for e in build_plan(spec).entries}
    assert entries["0013"].presentation == "tabRoot" and not entries["0013"].onboarding


def test_screen_states_inherit_their_base() -> None:
    spec = _tabbed()
    spec["screens"] += [
        {"id": "0009", "name": "Water Eject - Running", "state_of": "0008"},
        {"id": "0011p", "name": "Home - Pro", "state_of": "0011"},
        {"id": "0001b", "name": "Paywall - Annual", "state_of": "0001"},
    ]
    entries = {e.screen_id: e for e in build_plan(spec).entries}
    assert entries["0009"].presentation == "push" and entries["0009"].tab_root == "0011"
    assert entries["0011p"].presentation == "push" and entries["0011p"].tab_root == "0011"
    assert entries["0001b"].presentation == "fullScreenCover" and entries["0001b"].tab_root is None
