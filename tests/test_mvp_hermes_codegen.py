"""Tests for the Hermes-orchestrated Stage 3 codegen (Variant A).

The Hermes CLI is mocked (no real orchestration): subprocess.run is replaced with
a fake that writes a minimal flutter_app/ into the workspace, mirroring how the
live orchestrator would leave the directory.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from iosforge.mvp import hermes_codegen
from iosforge.mvp.paths import RunPaths

_APP_SPEC = {
    "app_name": "Todo",
    "app_type": "productivity",
    "screens": [{"id": "0000", "name": "Home", "route": "/home"}],
    "content": {"persistence": "local", "data_model": []},
    "backend": {"backend_needed": False},
}
_TASKS = {"tasks": [{"id": "t-scaffold", "type": "scaffold", "title": "Scaffold", "deps": []}]}


def _make_paths(tmp_path: Path) -> RunPaths:
    rp = RunPaths.create(tmp_path)
    (rp.screens_dir / "0000.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    rp.screens_json.write_text(json.dumps({"package": "com.x", "screens": [{"id": "0000"}]}))
    rp.app_spec_json.write_text(json.dumps(_APP_SPEC))
    rp.tasks_json.write_text(json.dumps(_TASKS))
    rp.handoff_dir.mkdir(parents=True, exist_ok=True)
    (rp.handoff_dir / "requirements.md").write_text("# reqs\n")
    return rp


def _fake_hermes(captured: dict[str, Any], *, build: bool = True) -> Any:
    def _run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        captured["cmd"] = cmd
        cwd = Path(kwargs["cwd"])
        if build:
            app = cwd / "flutter_app" / "lib"
            app.mkdir(parents=True, exist_ok=True)
            (cwd / "flutter_app" / "pubspec.yaml").write_text("name: todo\n")
            (app / "main.dart").write_text("void main() {}\n")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    return _run


def test_generate_via_hermes_stages_and_builds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rp = _make_paths(tmp_path)
    captured: dict[str, Any] = {}
    monkeypatch.setattr(hermes_codegen.subprocess, "run", _fake_hermes(captured))

    out = hermes_codegen.generate_via_hermes(rp)

    assert out == rp.flutter_app
    assert (rp.flutter_app / "pubspec.yaml").exists()
    assert (rp.flutter_app / "lib" / "main.dart").exists()

    # contract fully staged for the orchestrator
    assert (rp.claude_ws / "CONSTITUTION.md").exists()
    assert "go_router" in (rp.claude_ws / "CONSTITUTION.md").read_text()
    assert (rp.claude_ws / "app_spec.json").exists()
    assert (rp.claude_ws / "tasks.json").exists()
    assert (rp.claude_ws / "handoff" / "requirements.md").exists()
    assert (rp.claude_ws / "screens" / "0000.png").exists()

    # hermes invoked headless, orchestrating a parallel screen layer
    assert captured["cmd"][0] == hermes_codegen.HERMES_BIN
    assert "-z" in captured["cmd"] and "--yolo" in captured["cmd"]
    prompt = captured["cmd"][2]
    assert "worktree" in prompt
    assert "up to 4" in prompt  # default max_parallel surfaced in the prompt


def test_generate_via_claude_orchestrator_stages_and_builds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rp = _make_paths(tmp_path)
    captured: dict[str, Any] = {}
    monkeypatch.setattr(hermes_codegen.subprocess, "run", _fake_hermes(captured))

    out = hermes_codegen.generate_via_claude_orchestrator(rp)

    assert out == rp.flutter_app
    assert (rp.flutter_app / "pubspec.yaml").exists()
    assert (rp.flutter_app / "lib" / "main.dart").exists()

    # same staged contract as the Hermes path
    assert (rp.claude_ws / "CONSTITUTION.md").exists()
    assert (rp.claude_ws / "app_spec.json").exists()
    assert (rp.claude_ws / "tasks.json").exists()
    assert (rp.claude_ws / "handoff" / "requirements.md").exists()
    assert (rp.claude_ws / "screens" / "0000.png").exists()

    # the local Claude agent is invoked headless as the orchestrator/builder
    assert captured["cmd"][0] == hermes_codegen.CLAUDE_BIN
    assert "-p" in captured["cmd"] and "acceptEdits" in captured["cmd"]
    prompt = captured["cmd"][2]
    assert "up to 4" in prompt  # default max_parallel surfaced in the prompt
    assert "do NOT shell out to another `claude`" in prompt


def test_missing_tasks_raises(tmp_path: Path) -> None:
    rp = _make_paths(tmp_path)
    rp.tasks_json.unlink()
    with pytest.raises(RuntimeError, match="tasks.json missing"):
        hermes_codegen.generate_via_hermes(rp)
    with pytest.raises(RuntimeError, match="tasks.json missing"):
        hermes_codegen.generate_via_claude_orchestrator(rp)


def test_no_flutter_app_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rp = _make_paths(tmp_path)
    captured: dict[str, Any] = {}
    monkeypatch.setattr(hermes_codegen.subprocess, "run", _fake_hermes(captured, build=False))
    with pytest.raises(RuntimeError, match="did not produce a valid flutter_app"):
        hermes_codegen.generate_via_hermes(rp)
