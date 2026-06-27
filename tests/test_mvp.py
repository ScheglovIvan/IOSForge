"""Smoke tests for the MVP vertical — pure logic on the critical path.

No emulator / no Claude CLI here (those are live integration, exercised by a real run).
Covers: uiautomator XML -> screen elements, bounds math, run layout, claude workspace prep,
and the staged B->C->D entry-point orchestration with the claude subprocess mocked.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from iosforge.mvp import claude_gen, cli, crawl
from iosforge.mvp.paths import RunPaths

_SAMPLE_UI_XML = """<?xml version='1.0' encoding='UTF-8'?>
<hierarchy rotation="0">
  <node text="Play" resource-id="com.x:id/play" clickable="true" bounds="[0,0][100,50]"/>
  <node text="" resource-id="com.x:id/menu" clickable="true" bounds="[200,0][300,50]"/>
  <node text="label" resource-id="com.x:id/lbl" clickable="false" bounds="[0,60][100,90]"/>
</hierarchy>
"""


def test_clickables_extracts_only_clickable_nodes_with_bounds() -> None:
    els = crawl._clickables(_SAMPLE_UI_XML)
    assert len(els) == 2  # the non-clickable label is excluded
    assert {e["resource_id"] for e in els} == {"com.x:id/play", "com.x:id/menu"}
    assert els[0]["text"] == "Play"


def test_center_parses_bounds() -> None:
    assert crawl._center("[0,0][100,50]") == (50, 25)
    assert crawl._center("not-bounds") is None


def test_signature_is_stable_and_order_independent() -> None:
    a = crawl._clickables(_SAMPLE_UI_XML)
    b = list(reversed(a))
    assert crawl._signature(a) == crawl._signature(b)


def test_run_paths_layout(tmp_path: Path) -> None:
    rp = RunPaths.create(tmp_path)
    assert rp.screens_dir.is_dir()
    assert rp.screens_dir == rp.run_dir / "screens"
    assert rp.screens_json == rp.run_dir / "screens.json"
    assert rp.flutter_app == rp.run_dir / "flutter_app"


def test_prepare_workspace_copies_inputs_and_prompt(tmp_path: Path) -> None:
    rp = RunPaths.create(tmp_path)
    (rp.screens_dir / "0000.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    rp.screens_json.write_text('{"package":"com.x","screens":[]}')

    claude_gen._prepare_workspace(rp)

    assert (rp.claude_ws / "screens" / "0000.png").exists()
    assert (rp.claude_ws / "screens.json").read_text().startswith('{"package"')
    assert "Flutter app" in (rp.claude_ws / "PROMPT.md").read_text()


_APP_SPEC = {
    "app_name": "Todo",
    "package": "com.example.todo",
    "screens": [{"id": "0000", "name": "Home", "screenshot": "screens/0000.png"}],
    "flows": [],
    "data_model": [],
    "design": {"primary_color": "#3366FF", "theme": "light"},
}

_TASKS = {
    "tasks": [
        {"id": "t-scaffold", "type": "scaffold", "title": "Scaffold", "screens": [], "deps": []},
        {
            "id": "t-home",
            "type": "screen",
            "title": "Home",
            "screens": ["0000"],
            "deps": ["t-scaffold"],
        },
    ]
}


def _staged_claude() -> Any:
    """A single claude stand-in dispatching across the analyze/decompose/task prompts."""

    def _run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        cwd = Path(kwargs["cwd"])
        prompt = cmd[2]
        if "Current task:" in prompt:
            app = cwd / "flutter_app"
            if "- type: scaffold" in prompt:
                (app / "lib").mkdir(parents=True, exist_ok=True)
                (app / "pubspec.yaml").write_text("name: todo\n")
                (app / "lib" / "main.dart").write_text("void main() {}\n")
            else:
                (app / "lib" / "screens").mkdir(parents=True, exist_ok=True)
                (app / "lib" / "screens" / "home.dart").write_text("// screen\n")
        elif "`tasks.json`" in prompt:
            (cwd / "tasks.json").write_text(json.dumps(_TASKS))
        elif "`app_spec.json`" in prompt:
            (cwd / "app_spec.json").write_text(json.dumps(_APP_SPEC))
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    return _run


def test_cli_run_uses_staged_pipeline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    apk = tmp_path / "app.apk"
    apk.write_bytes(b"PK\x03\x04")

    def _fake_walk(paths: RunPaths, package: str, max_screens: int) -> dict[str, object]:
        (paths.screens_dir / "0000.png").write_bytes(b"\x89PNG\r\n\x1a\n")
        result = {"package": package, "screens": [{"id": "0000"}]}
        paths.screens_json.write_text(json.dumps(result))
        return result

    monkeypatch.setattr(cli.emulator, "start_emulator", lambda avd: None)
    monkeypatch.setattr(cli.emulator, "wait_for_boot", lambda: None)
    monkeypatch.setattr(cli.emulator, "install_apk", lambda apk: "com.example.todo")
    monkeypatch.setattr(cli.emulator, "launch", lambda package: None)
    monkeypatch.setattr(cli.crawl, "walk", _fake_walk)
    monkeypatch.setattr(subprocess, "run", _staged_claude())

    flutter_app = cli.run(apk, tmp_path / "runs", max_screens=2, avd="mvp", do_build_check=False)

    assert flutter_app.name == "flutter_app"
    assert (flutter_app / "pubspec.yaml").exists()
    assert (flutter_app / "lib" / "main.dart").exists()
    assert (flutter_app / "lib" / "screens" / "home.dart").exists()
    run_dir = flutter_app.parent
    assert flutter_app == run_dir / "flutter_app"
    assert (run_dir / "app_spec.json").exists()
    assert (run_dir / "tasks.json").exists()
