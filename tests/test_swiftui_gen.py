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
from iosforge.mvp.swiftui_scaffold import screen_entries
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
    rounds = iter([(["App/X.swift:1:1: error: boom"], "log1"), ([], "** BUILD SUCCEEDED **")])
    monkeypatch.setattr(swiftui_gen, "compile_errors", lambda *a: next(rounds))
    prompts: list[str] = []
    monkeypatch.setattr(
        swiftui_gen.claude_gen, "run_task", lambda ws, prompt, **k: prompts.append(prompt) or 0
    )

    errors, log = swiftui_gen.ensure_compiles(paths, "Demo", attempts=2)

    assert errors == [] and "SUCCEEDED" in log
    assert len(prompts) == 1 and "App/X.swift:1:1: error: boom" in prompts[0]


def test_compile_gate_gives_up_after_attempts(
    paths: RunPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(swiftui_gen, "compile_errors", lambda *a: (["e: still broken"], ""))
    prompts: list[str] = []
    monkeypatch.setattr(
        swiftui_gen.claude_gen, "run_task", lambda ws, prompt, **k: prompts.append(prompt) or 0
    )
    errors, _ = swiftui_gen.ensure_compiles(paths, "Demo", attempts=2)
    assert errors == ["e: still broken"] and len(prompts) == 2


def test_compile_errors_include_permission_lint_without_toolchain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(xcode, "toolchain_available", lambda: False)
    view = tmp_path / "App/Features/0013/Screen0013View.swift"
    view.parent.mkdir(parents=True)
    view.write_text("let m = CMPedometer()\n")
    errors, log = swiftui_gen.compile_errors(tmp_path, "Demo", tmp_path / "dd")
    assert log == "" and len(errors) == 1 and "motion permission API" in errors[0]


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


@pytest.mark.mac
@pytest.mark.skipif(not xcode.toolchain_available(), reason="needs XcodeGen + xcodebuild")
def test_scaffold_builds_with_xcodebuild(paths: RunPaths) -> None:
    swiftui_gen.prepare_workspace(paths, app_name="Speaker Test", bundle_id="com.example.s")
    errors, log = swiftui_gen.compile_errors(
        paths.claude_ws / "xcode_app", "SpeakerTest", paths.run_dir / "DerivedData"
    )
    assert errors == [], errors
    assert "BUILD SUCCEEDED" in log
