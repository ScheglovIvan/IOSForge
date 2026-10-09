"""SwiftUI codegen slice: orchestration, compile-gate fix loop and prompts (no toolchain)."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from iosforge.mvp import swiftui_gen, xcode
from iosforge.mvp.paths import RunPaths
from iosforge.mvp.swiftui_prompts import compile_fix_prompt, screen_prompt, theme_prompt
from iosforge.mvp.swiftui_scaffold import AppIdentity, screen_entries
from tests.test_feasibility import _spec_three_screens

SPEC: dict[str, Any] = {
    "app_name": "Speaker Test",
    "screens": [
        {"id": "0011", "name": "Home - Test Hub", "route": "/", "navigates_to": ["0013", "9999"]},
        {"id": "0013", "name": "Sound-Level Meter", "navigates_to": ["0011"]},
    ],
    "navigation": {"map": [{"from": "0011", "to": "0013"}]},
}


@pytest.fixture
def paths(tmp_path: Path) -> RunPaths:
    rp = RunPaths.create(tmp_path / "runs")
    rp.app_spec_json.write_text(json.dumps(SPEC))
    return rp


def _fake_task(calls: list[str]) -> Any:
    def run(workspace: Path, prompt: str, **_: Any) -> int:
        calls.append(prompt)
        for line in prompt.splitlines():
            if line.startswith("TASK: implement screen `"):
                sid = line.split("`")[1]
                view = workspace / f"xcode_app/App/Features/{sid}/Screen{sid}View.swift"
                view.write_text(
                    f'struct Screen{sid}View: View {{ var body: some View {{ Text("{sid}") }} }}\n'
                )
        return 0

    return run


def test_generate_runs_theme_then_each_screen(
    paths: RunPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(swiftui_gen.claude_gen, "run_task", _fake_task(calls))
    monkeypatch.setattr(xcode, "toolchain_available", lambda: False)

    result = swiftui_gen.generate(paths, app_name="Speaker Test", bundle_id="com.example.s")

    assert result.errors == []
    assert result.scheme == "SpeakerTest"
    assert "TASK: theme" in calls[0]
    screens = [re.search(r"TASK: implement screen `([^`]+)`", c) for c in calls[1:]]
    assert [m.group(1) for m in screens if m] == ["0011", "0013"]
    assert (paths.xcode_app / "App/Navigation/ScreenID.swift").exists()
    assert (paths.claude_ws / "app_spec.json").exists()
    assert (paths.claude_ws / "reference/Spike/SpikeApp.swift").exists()


def test_generate_reports_screens_left_as_placeholders(
    paths: RunPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(swiftui_gen.claude_gen, "run_task", lambda *a, **k: 0)
    monkeypatch.setattr(xcode, "toolchain_available", lambda: False)
    result = swiftui_gen.generate(paths, app_name="Speaker Test", bundle_id="com.example.s")
    assert result.errors == ["screen 0011 not generated", "screen 0013 not generated"]


def test_compile_gate_loops_fix_task_until_clean(
    paths: RunPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    rounds = iter(
        [
            swiftui_gen.GateCheck(["App/X.swift:1:1: error: boom"], "log1", ["App/App.swift"]),
            swiftui_gen.GateCheck([], "** BUILD SUCCEEDED **"),
        ]
    )
    monkeypatch.setattr(swiftui_gen, "compile_errors", lambda *a: next(rounds))
    prompts: list[str] = []
    monkeypatch.setattr(
        swiftui_gen.claude_gen, "run_task", lambda ws, prompt, **k: prompts.append(prompt) or 0
    )

    gate = swiftui_gen.ensure_compiles(paths, AppIdentity("Demo", "b.c"), attempts=2)

    assert gate.errors == [] and "SUCCEEDED" in gate.log
    assert gate.restored == ["App/App.swift"]
    assert len(prompts) == 1 and "App/X.swift:1:1: error: boom" in prompts[0]


def test_compile_gate_gives_up_after_attempts(
    paths: RunPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        swiftui_gen, "compile_errors", lambda *a: swiftui_gen.GateCheck(["e: still broken"])
    )
    prompts: list[str] = []
    monkeypatch.setattr(
        swiftui_gen.claude_gen, "run_task", lambda ws, prompt, **k: prompts.append(prompt) or 0
    )
    gate = swiftui_gen.ensure_compiles(paths, AppIdentity("Demo", "b.c"), attempts=2)
    assert gate.errors == ["e: still broken"] and len(prompts) == 2


def test_compile_errors_include_permission_lint_without_toolchain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(xcode, "toolchain_available", lambda: False)
    swiftui_gen.write_scaffold(tmp_path, SPEC, app_name="Demo", bundle_id="b.c")
    (tmp_path / "App/Features/0013/Screen0013View.swift").write_text("let m = CMPedometer()\n")
    check = swiftui_gen.compile_errors(tmp_path, SPEC, AppIdentity("Demo", "b.c"), tmp_path / "dd")
    assert check.log == "" and len(check.errors) == 1 and "motion permission API" in check.errors[0]


def test_model_tampering_with_contract_is_restored_and_reported(
    paths: RunPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    fake = _fake_task(calls)

    def tampering(workspace: Path, prompt: str, **kw: Any) -> int:
        router = workspace / "xcode_app/App/Navigation/Router.swift"
        router.write_text("final class Router {}\n")
        return int(fake(workspace, prompt, **kw))

    monkeypatch.setattr(swiftui_gen.claude_gen, "run_task", tampering)
    monkeypatch.setattr(xcode, "toolchain_available", lambda: False)

    result = swiftui_gen.generate(paths, app_name="Speaker Test", bundle_id="com.example.s")

    assert result.contract_restored == ["App/Navigation/Router.swift"]
    assert (
        "func open(_ raw: String)" in (paths.xcode_app / "App/Navigation/Router.swift").read_text()
    )


def test_scope_to_prunes_spec_to_requested_screens(paths: RunPaths) -> None:
    paths.app_spec_json.write_text(json.dumps(_spec_three_screens()))
    swiftui_gen._scope_to(paths, ["0000"])
    spec = json.loads(paths.app_spec_json.read_text())
    assert [s["id"] for s in spec["screens"]] == ["0000"]
    assert spec["screens"][0]["navigates_to"] == []
    assert spec["navigation"]["map"] == []


def test_screen_prompt_carries_contract_and_inputs() -> None:
    entries = screen_entries(SPEC)
    prompt = screen_prompt(entries[0], targets=[entries[1]])
    assert "TASK: implement screen `0011`" in prompt
    for needle in (
        "screens/0011.png",
        "source/0011.json",
        "App/Features/0011/Screen0011View.swift",
        "Fixtures0011.swift",
        "`.s0013` — Sound-Level Meter (push)",
        "Permissions.request(",
        "Headless.isActive",
        "NEVER edit scaffold files",
        "CONTENT DIVERGENCE",
    ):
        assert needle in prompt
    lonely = screen_prompt(entries[0], targets=[], diverge_content=False)
    assert "No other screen exists" in lonely and "CONTENT DIVERGENCE" not in lonely


def test_theme_and_fix_prompts() -> None:
    assert "design_tokens" in theme_prompt(app_name="Speaker Test")
    fix = compile_fix_prompt(["App/X.swift:1:1: error: boom"])
    assert "App/X.swift:1:1: error: boom" in fix and "PermissionPrompter" in fix


RICH_SPEC: dict[str, Any] = {
    "app_name": "Rich Demo",
    "screens": [
        {"id": "0000", "name": "Splash / Launch"},
        {"id": "0001", "name": "Paywall - Pro"},
        {"id": "0002", "name": "Full Player", "presentation": "fullScreenCover"},
        {"id": "0011", "name": "Home", "route": "/"},
        {"id": "0013", "name": "Sound-Level Meter"},
        {"id": "default", "name": 'Keyword "id"\nwith newline'},
    ],
    "permissions": [{"permission": "Microphone", "reason": "Measures sound."}],
}

_PROMPTER = """import AVFoundation

enum MicrophonePermission: PermissionPrompter {
    static let kind: PermissionKind = .microphone
    static func prompt() async -> Bool { await AVAudioApplication.requestRecordPermission() }
}
"""

_METER_VIEW = """import SwiftUI

struct Screen0013View: View {
    @Environment(Router.self) private var router

    var body: some View {
        Button("Measure") {
            Task { await Permissions.request(MicrophonePermission.self) }
            router.show(.s0001)
        }
        .font(.custom("Demo-Regular", size: 17))
    }
}
"""


@pytest.mark.mac
@pytest.mark.skipif(not xcode.toolchain_available(), reason="needs XcodeGen + xcodebuild")
def test_rich_scaffold_builds_with_xcodebuild(tmp_path: Path) -> None:
    fonts = tmp_path / "fonts"
    fonts.mkdir()
    (fonts / "Demo-Regular.ttf").write_bytes(b"\x00\x01\x00\x00")
    media = tmp_path / "media"
    media.mkdir()
    (media / "hero.png").write_bytes(b"\x89PNG")
    app = tmp_path / "xcode_app"
    identity = AppIdentity("Rich Demo", "com.example.rich")
    swiftui_gen.write_scaffold(
        app, RICH_SPEC, app_name=identity.app_name, bundle_id=identity.bundle_id,
        fonts_dir=fonts, media_dir=media,
    )  # fmt: skip
    (app / "App/Permissions/MicrophonePermission.swift").write_text(_PROMPTER)
    (app / "App/Features/0013/Screen0013View.swift").write_text(_METER_VIEW)

    check = swiftui_gen.compile_errors(app, RICH_SPEC, identity, tmp_path / "DerivedData")

    assert check.errors == [], check.errors
    assert check.restored == []
    assert "BUILD SUCCEEDED" in check.log
