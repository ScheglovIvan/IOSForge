"""SwiftUI codegen: task DAG, sandbox harvest, compile gate and prompts (toolchain mocked)."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from iosforge.mvp import swiftui_gen, xcode
from iosforge.mvp.paths import RunPaths
from iosforge.mvp.swiftui_permissions import PERMISSION_KINDS
from iosforge.mvp.swiftui_prompts import (
    compile_fix_prompt,
    components_prompt,
    screen_prompt,
    theme_prompt,
)
from iosforge.mvp.swiftui_scaffold import AppIdentity, build_plan
from tests.test_feasibility import _spec_three_screens

SPEC: dict[str, Any] = {
    "app_name": "Speaker Test",
    "screens": [
        {"id": "0011", "name": "Home - Test Hub", "route": "/", "navigates_to": ["0013", "9999"]},
        {"id": "0013", "name": "Sound-Level Meter", "navigates_to": ["0011"]},
    ],
    "navigation": {"type": "stack", "map": [{"from": "0011", "to": "0013"}], "deep_links": []},
    "permissions": [{"permission": "Microphone", "reason": "Measure."}],
}


@pytest.fixture
def paths(tmp_path: Path) -> RunPaths:
    rp = RunPaths.create(tmp_path / "runs")
    rp.app_spec_json.write_text(json.dumps(SPEC))
    return rp


def _task_of(prompt: str) -> str:
    match = re.search(r"TASK: implement screen `([^`]+)`", prompt)
    if match:
        return f"screen-{match.group(1)}"
    return (
        "theme"
        if "TASK: theme" in prompt
        else "components"
        if "TASK: component" in prompt
        else "fix"
    )


def _fake_task(calls: list[tuple[str, Path]]) -> Any:
    def run(workspace: Path, prompt: str, **_: Any) -> int:
        task = _task_of(prompt)
        calls.append((task, workspace))
        app = workspace / "xcode_app"
        if task.startswith("screen-"):
            sid = task.removeprefix("screen-")
            (app / f"App/Features/{sid}/Screen{sid}View.swift").write_text(
                f'struct Screen{sid}View: View {{ var body: some View {{ Text("{sid}") }} }}\n'
            )
            (app / f"App/Fixtures/Fixtures{sid}.swift").write_text("extension Fixtures {}\n")
            (app / "App/Theme/Theme.swift").write_text("enum Theme { static let hacked = 1 }\n")
            (app / "App/Navigation/Evil.swift").write_text("struct Evil {}\n")
        elif task == "theme":
            (app / "App/Theme/Theme.swift").write_text("enum Theme { static let pad = 8 }\n")
        elif task == "components":
            (app / "App/Components/Card.swift").write_text("struct Card {}\n")
        return 0

    return run


def test_generate_runs_dag_and_harvests_only_owned_paths(
    paths: RunPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, Path]] = []
    monkeypatch.setattr(swiftui_gen.claude_gen, "run_task", _fake_task(calls))
    monkeypatch.setattr(xcode, "toolchain_available", lambda: False)

    result = swiftui_gen.generate(paths, app_name="Speaker Test", bundle_id="com.example.s")

    assert result.errors == []
    tasks = [t for t, _ in calls]
    assert tasks[:2] == ["theme", "components"]
    assert sorted(tasks[2:]) == ["screen-0011", "screen-0013"]
    assert all(ws == paths.claude_ws for t, ws in calls if not t.startswith("screen-"))
    assert all("sandboxes" in str(ws) for t, ws in calls if t.startswith("screen-"))
    app = paths.xcode_app
    assert 'Text("0013")' in (app / "App/Features/0013/Screen0013View.swift").read_text()
    assert (app / "App/Fixtures/Fixtures0011.swift").exists()
    assert "pad = 8" in (app / "App/Theme/Theme.swift").read_text()
    assert not (app / "App/Navigation/Evil.swift").exists()
    screen_runs = {t.task: t for t in result.tasks if t.task.startswith("screen-")}
    assert screen_runs["screen-0011"].ignored == [
        "App/Navigation/Evil.swift",
        "App/Theme/Theme.swift",
    ]
    assert not (paths.run_dir / "sandboxes" / "0011").exists()
    summary = swiftui_gen.report(result)
    assert summary["tabs"] == [{"case": "t0011", "title": "Home", "root": "0011"}]


def test_generate_reports_screens_left_as_placeholders(
    paths: RunPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(swiftui_gen.claude_gen, "run_task", lambda *a, **k: 0)
    monkeypatch.setattr(xcode, "toolchain_available", lambda: False)
    result = swiftui_gen.generate(paths, app_name="Speaker Test", bundle_id="com.example.s")
    assert result.errors == ["screen 0011 not generated", "screen 0013 not generated"]


def test_fix_task_tampering_is_restored_and_reported(
    paths: RunPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, Path]] = []
    fake = _fake_task(calls)
    rounds = iter([True, False])

    def run(workspace: Path, prompt: str, **kw: Any) -> int:
        if _task_of(prompt) == "fix":
            (workspace / "xcode_app/App/Navigation/Router.swift").write_text(
                "final class Router {}\n"
            )
            return 0
        return int(fake(workspace, prompt, **kw))

    def gate(app_dir: Path, spec: dict[str, Any], identity: AppIdentity, dd: Path) -> Any:
        check = swiftui_gen.GateCheck([])
        real = swiftui_gen.enforce_contract(app_dir, spec, identity)
        check.contract = real
        if next(rounds):
            check.errors = ["App/Features/0013/Screen0013View.swift:3:1: error: boom"]
        return check

    monkeypatch.setattr(swiftui_gen.claude_gen, "run_task", run)
    monkeypatch.setattr(swiftui_gen, "compile_errors", gate)

    result = swiftui_gen.generate(paths, app_name="Speaker Test", bundle_id="com.example.s")

    assert result.errors == []
    assert result.fix_rounds == [swiftui_gen.FixRound(1, 1, {"0013": 1})]
    assert result.contract_restored == ["App/Navigation/Router.swift"]
    assert (
        "func open(_ raw: String)" in (paths.xcode_app / "App/Navigation/Router.swift").read_text()
    )


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
    gate, rounds = swiftui_gen.ensure_compiles(
        paths, AppIdentity("Demo", "b.c"), build_plan(SPEC), prompters=[], attempts=2
    )
    assert gate.errors == ["e: still broken"] and len(prompts) == 2
    assert [r.screens for r in rounds] == [{"shared": 1}, {"shared": 1}]


def test_screen_of_error() -> None:
    plan = build_plan(SPEC)
    assert swiftui_gen.screen_of_error("App/Features/0013/X.swift:1:1: error: e", plan) == "0013"
    assert (
        swiftui_gen.screen_of_error("App/Fixtures/Fixtures0011.swift:2: error: e", plan) == "0011"
    )
    assert swiftui_gen.screen_of_error("App/Theme/Theme.swift:1: error: e", plan) == "shared"


def test_compile_errors_without_toolchain_cover_contract_and_lint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(xcode, "toolchain_available", lambda: False)
    swiftui_gen.write_scaffold(tmp_path, SPEC, app_name="Demo", bundle_id="b.c")
    (tmp_path / "App/Features/0013/Screen0013View.swift").write_text(
        "Task { await Permissions.request(CameraPermission.self) }\n"
    )
    (tmp_path / "App/Components/AppTabBar.swift").write_text("let r = Router()\n")
    (tmp_path / "App/Services").mkdir()

    check = swiftui_gen.compile_errors(tmp_path, SPEC, AppIdentity("Demo", "b.c"), tmp_path / "dd")

    assert check.log == ""
    assert len(check.errors) == 3
    assert check.errors[0].startswith("App/Services: error: unexpected path")
    assert "AppTabBar must only assign" in check.errors[1]
    assert "`CameraPermission` is not declared" in check.errors[2]


def test_scope_to_prunes_spec_to_requested_screens(paths: RunPaths) -> None:
    paths.app_spec_json.write_text(json.dumps(_spec_three_screens()))
    swiftui_gen._scope_to(paths, ["0000"])
    spec = json.loads(paths.app_spec_json.read_text())
    assert [s["id"] for s in spec["screens"]] == ["0000"]
    assert spec["navigation"]["map"] == []


def _tabbed_spec() -> dict[str, Any]:
    def bar(active: str) -> list[dict[str, str]]:
        titles = ["Cleaner", "Meter"]
        data = " / ".join(f"{t} (active)" if t == active else t for t in titles)
        return [{"type": "tab_bar", "data": data}]

    return {
        "app_name": "Rich Demo",
        "screens": [
            {"id": "0000", "name": "Splash / Launch"},
            {"id": "0001", "name": "Paywall - Pro"},
            {"id": "0002", "name": "Full Player", "presentation": "fullScreenCover"},
            {"id": "0011", "name": "Home", "components": bar("Cleaner"), "navigates_to": ["0008"]},
            {"id": "0013", "name": "Sound-Level Meter", "components": bar("Meter")},
            {"id": "0008", "name": 'Keyword "id"\nwith newline'},
            {"id": "default", "name": "Default"},
        ],
        "navigation": {
            "type": "tab_bar_with_stack",
            "map": [{"from": "0013", "to": "default", "via": "details"}],
            "deep_links": [],
        },
        "permissions": [{"permission": k.case.replace("_", " ")} for k in PERMISSION_KINDS],
    }


def test_prompts_carry_contract_and_placement() -> None:
    plan = build_plan(_tabbed_spec())
    entries = {e.screen_id: e for e in plan.entries}
    names = swiftui_gen.prompter_names(_tabbed_spec())
    assert len(names) == len(PERMISSION_KINDS)

    root = screen_prompt(entries["0013"], plan, targets=[entries["default"]], prompters=names)
    assert "TASK: implement screen `0013`" in root
    assert "root of the 'Meter' tab" in root and "do NOT draw a tab bar" in root
    assert "`.sDefault` — Default (push)" in root
    for needle in (
        "source/0013.json",
        "Fixtures0013.swift",
        "COMPONENTS.md",
        "`MicrophonePermission`",
    ):
        assert needle in root
    pushed = screen_prompt(
        entries["0008"], plan, targets=[], prompters=names, diverge_content=False
    )
    assert "pushed onto the 'Cleaner' tab" in pushed and "CONTENT DIVERGENCE" not in pushed
    assert "presented modally (fullScreenCover)" in screen_prompt(
        entries["0001"], plan, targets=[], prompters=[]
    )
    inferred = screen_prompt(entries["0013"], plan, targets=[], prompters=[], observed=False)
    assert "NO screenshot" in inferred and "screens/0013.png" not in inferred
    assert "onboarding screen" in screen_prompt(entries["0000"], plan, targets=[], prompters=[])
    assert "none (the app declares no permissions)" in theme_prompt(app_name="A", prompters=[])

    components = components_prompt(plan, prompters=names)
    assert "struct AppTabBar: View {{" not in components
    assert (
        "struct AppTabBar: View { let tabs: [AppTab]; @Binding var selection: AppTab }"
        in components
    )
    assert "`.t0013` — 'Meter'" in components
    assert "has no tab bar" in components_prompt(build_plan(SPEC), prompters=[])
    fix = compile_fix_prompt(["App/X.swift:1:1: error: boom"], prompters=names)
    assert "App/X.swift:1:1: error: boom" in fix and "Unexpected-path errors" in fix


_METER_SERVICE = """import AVFoundation

final class LevelMeterService {
    private let engine = AVAudioEngine()

    func start() async {
        guard !Headless.isActive else { return }
        guard await Permissions.request(MicrophonePermission.self) else { return }
        _ = engine.inputNode
    }
}
"""

_METER_VIEW = """import SwiftUI

struct Screen0013View: View {
    @Environment(Router.self) private var router
    private let service = LevelMeterService()

    var body: some View {
        Button("Measure") {
            Task { await service.start() }
            router.show(.sDefault)
            router.show(.s0002)
        }
        .font(.custom("Demo-Regular", size: 17))
    }
}
"""


@pytest.mark.mac
@pytest.mark.skipif(not xcode.toolchain_available(), reason="needs XcodeGen + xcodebuild")
def test_tabbed_scaffold_with_every_prompter_builds(tmp_path: Path) -> None:
    fonts = tmp_path / "fonts"
    fonts.mkdir()
    (fonts / "Demo-Regular.ttf").write_bytes(b"\x00\x01\x00\x00")
    media = tmp_path / "media"
    media.mkdir()
    (media / "hero.png").write_bytes(b"\x89PNG")
    app = tmp_path / "xcode_app"
    spec = _tabbed_spec()
    identity = AppIdentity("Rich Demo", "com.example.rich")
    swiftui_gen.write_scaffold(
        app, spec, app_name=identity.app_name, bundle_id=identity.bundle_id,
        fonts_dir=fonts, media_dir=media,
    )  # fmt: skip
    (app / "App/Features/0013/LevelMeterService.swift").write_text(_METER_SERVICE)
    (app / "App/Features/0013/Screen0013View.swift").write_text(_METER_VIEW)

    check = swiftui_gen.compile_errors(app, spec, identity, tmp_path / "DerivedData")

    assert check.errors == [], check.errors
    assert check.contract.tampered == []
    assert len(list((app / "App/Permissions").iterdir())) == len(PERMISSION_KINDS) + 1
    assert "BUILD SUCCEEDED" in check.log


def test_ignored_writes_measured_against_sandbox_snapshot(
    paths: RunPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, Path]] = []
    fake = _fake_task(calls)

    def honest(workspace: Path, prompt: str, **kw: Any) -> int:
        task = _task_of(prompt)
        if not task.startswith("screen-"):
            return int(fake(workspace, prompt, **kw))
        sid = task.removeprefix("screen-")
        other = "0013" if sid == "0011" else "0011"
        app = workspace / "xcode_app"
        (app / f"App/Features/{sid}/Screen{sid}View.swift").write_text(
            f'struct Screen{sid}View: View {{ var body: some View {{ Text("{sid}") }} }}\n'
        )
        ws_view = paths.claude_ws / f"xcode_app/App/Features/{other}/Screen{other}View.swift"
        ws_view.write_text(
            f"struct Screen{other}View: View {{ var body: some View {{ EmptyView() }} }}\n"
        )
        return 0

    monkeypatch.setattr(swiftui_gen.claude_gen, "run_task", honest)
    monkeypatch.setattr(xcode, "toolchain_available", lambda: False)
    monkeypatch.setattr(swiftui_gen, "ThreadPoolExecutor", _SerialPool)

    result = swiftui_gen.generate(paths, app_name="Speaker Test", bundle_id="com.example.s")

    assert all(t.ignored == [] for t in result.tasks)


class _SerialPool:
    def __init__(self, max_workers: int) -> None:
        self.max_workers = max_workers

    def __enter__(self) -> _SerialPool:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def submit(self, fn: Any, *args: Any, **kwargs: Any) -> Any:
        value = fn(*args, **kwargs)
        return type("Done", (), {"result": lambda self: value})()
