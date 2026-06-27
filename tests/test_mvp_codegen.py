"""Tests for the MVP task-runner codegen (Stage D, generate_from_tasks).

No real Claude CLI: subprocess.run is replaced with a fake that inspects the
per-task prompt and drops the files that task would create into the workspace
flutter_app/, recording the order of task ids it was invoked with.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any

import pytest

from iosforge.mvp import claude_gen
from iosforge.mvp.paths import RunPaths

_TASKS = {
    "tasks": [
        {
            "id": "t-scaffold",
            "type": "scaffold",
            "title": "Project + theme",
            "screens": [],
            "deps": [],
        },
        {
            "id": "t-add",
            "type": "screen",
            "title": "Add screen",
            "screens": ["0001"],
            "deps": ["t-scaffold", "t-home"],
        },
        {
            "id": "t-home",
            "type": "screen",
            "title": "Home screen",
            "screens": ["0000"],
            "deps": ["t-scaffold"],
        },
    ]
}

_APP_SPEC = {
    "app_name": "Todo",
    "package": "com.example.todo",
    "screens": [{"id": "0000", "name": "Home"}, {"id": "0001", "name": "Add"}],
    "flows": [],
    "data_model": [],
    "design": {"primary_color": "#3366FF", "theme": "light"},
}


def _field(prompt: str, name: str) -> str:
    m = re.search(rf"- {name}: (\S+)", prompt)
    assert m is not None, f"prompt missing {name}"
    return m.group(1)


def _make_paths(tmp_path: Path) -> RunPaths:
    rp = RunPaths.create(tmp_path)
    (rp.screens_dir / "0000.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    (rp.screens_dir / "0001.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    rp.screens_json.write_text(json.dumps({"package": "com.example.todo", "screens": []}))
    rp.app_spec_json.write_text(json.dumps(_APP_SPEC))
    rp.tasks_json.write_text(json.dumps(_TASKS))
    return rp


def _fake_runner(order: list[str], *, scaffold_writes: bool = True) -> Any:
    def _run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        cwd = Path(kwargs["cwd"])
        prompt = cmd[2]
        tid = _field(prompt, "id")
        ttype = _field(prompt, "type")
        order.append(tid)
        app = cwd / "flutter_app"
        if ttype == "scaffold" and scaffold_writes:
            (app / "lib").mkdir(parents=True, exist_ok=True)
            (app / "pubspec.yaml").write_text("name: todo\n")
            (app / "lib" / "main.dart").write_text("void main() {}\n")
        elif ttype == "screen":
            (app / "lib" / "screens").mkdir(parents=True, exist_ok=True)
            (app / "lib" / "screens" / f"{tid}.dart").write_text("// screen\n")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    return _run


def test_generate_from_tasks_runs_in_dependency_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rp = _make_paths(tmp_path)
    order: list[str] = []
    monkeypatch.setattr(claude_gen.subprocess, "run", _fake_runner(order))

    out = claude_gen.generate_from_tasks(rp)

    assert out == rp.flutter_app
    assert not (rp.claude_ws / "flutter_app").exists()
    assert (out / "pubspec.yaml").exists()
    assert (out / "lib" / "main.dart").exists()
    assert (out / "lib" / "screens" / "t-home.dart").exists()
    assert (out / "lib" / "screens" / "t-add.dart").exists()

    assert len(order) == 3
    deps = {str(t["id"]): {str(d) for d in t["deps"]} for t in _TASKS["tasks"]}
    for tid, required in deps.items():
        idx = order.index(tid)
        for dep in required:
            assert order.index(dep) < idx, f"{dep} must precede {tid}"


def test_generate_from_tasks_stages_all_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rp = _make_paths(tmp_path)
    monkeypatch.setattr(claude_gen.subprocess, "run", _fake_runner([]))

    claude_gen.generate_from_tasks(rp)

    assert (rp.claude_ws / "app_spec.json").exists()
    assert (rp.claude_ws / "tasks.json").exists()
    assert (rp.claude_ws / "screens" / "0000.png").exists()
    assert not (rp.claude_ws / "PROMPT.md").exists()


def test_generate_from_tasks_rejects_malformed_tasks_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rp = _make_paths(tmp_path)
    rp.tasks_json.write_text(json.dumps({"not_tasks": []}))
    monkeypatch.setattr(claude_gen.subprocess, "run", _fake_runner([]))

    with pytest.raises(RuntimeError, match="empty or missing 'tasks'"):
        claude_gen.generate_from_tasks(rp)


def test_generate_from_tasks_raises_when_scaffold_produces_no_pubspec(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rp = _make_paths(tmp_path)
    monkeypatch.setattr(claude_gen.subprocess, "run", _fake_runner([], scaffold_writes=False))

    with pytest.raises(RuntimeError, match="scaffold"):
        claude_gen.generate_from_tasks(rp)


def test_generate_from_tasks_requires_artifacts(tmp_path: Path) -> None:
    rp = RunPaths.create(tmp_path)
    with pytest.raises(RuntimeError, match="app_spec.json missing"):
        claude_gen.generate_from_tasks(rp)
