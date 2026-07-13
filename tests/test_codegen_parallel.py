"""Tests for the parallel screen layer of the claude task runner.

A stubbed `claude` writes each screen's file into ITS OWN worktree cwd under the
owned path `lib/features/<id>/`. Real `git` drives the worktree/merge machinery.
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

_REAL_RUN = subprocess.run  # captured before any monkeypatch replaces subprocess.run

_APP_SPEC = {"app_name": "Todo", "package": "com.example.todo", "screens": []}

_TASKS = {
    "tasks": [
        {"id": "t-scaffold", "type": "scaffold", "title": "S", "screens": [], "deps": []},
        {
            "id": "t-lib",
            "type": "component_library",
            "title": "L",
            "screens": [],
            "deps": ["t-scaffold"],
        },
        {"id": "t-a", "type": "screen", "title": "A", "screens": ["a"], "deps": ["t-lib"]},
        {"id": "t-b", "type": "screen", "title": "B", "screens": ["b"], "deps": ["t-lib"]},
        {"id": "t-c", "type": "screen", "title": "C", "screens": ["c"], "deps": ["t-lib"]},
    ]
}


def _field(prompt: str, name: str) -> str:
    m = re.search(rf"- {name}: (\S+)", prompt)
    assert m is not None
    return m.group(1)


def _make_paths(tmp_path: Path) -> RunPaths:
    rp = RunPaths.create(tmp_path)
    rp.screens_json.write_text(json.dumps({"package": "com.example.todo", "screens": []}))
    rp.app_spec_json.write_text(json.dumps(_APP_SPEC))
    rp.tasks_json.write_text(json.dumps(_TASKS))
    return rp


def _fake_runner(order: list[str], *, escape: str | None = None) -> Any:
    def _run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if not cmd or cmd[0] != claude_gen.CLAUDE_BIN:
            return _REAL_RUN(cmd, **kwargs)
        cwd = Path(kwargs["cwd"])
        prompt = cmd[2]
        tid = _field(prompt, "id")
        ttype = _field(prompt, "type")
        order.append(tid)
        app = cwd / "flutter_app"
        if ttype in ("scaffold", "component_library"):
            (app / "lib").mkdir(parents=True, exist_ok=True)
            (app / "pubspec.yaml").write_text("name: todo\n")
            (app / "lib" / "main.dart").write_text("void main() {}\n")
        else:
            for sid in ("a", "b", "c"):
                if f"screens/{sid}.png" in prompt or tid.endswith(sid):
                    feat = app / "lib" / "features" / sid
                    feat.mkdir(parents=True, exist_ok=True)
                    (feat / f"{sid}_screen.dart").write_text(f"// {sid}\n")
            if escape and tid == f"t-{escape}":
                (app / "lib" / "main.dart").write_text("void main() { /* hijacked */ }\n")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    return _run


def test_parallel_screens_build_and_merge_owned_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rp = _make_paths(tmp_path)
    order: list[str] = []
    monkeypatch.setattr(claude_gen.subprocess, "run", _fake_runner(order))

    out = claude_gen.generate_from_tasks(rp, max_parallel=3)

    assert out == rp.flutter_app
    assert not (out / ".git").exists()  # git metadata stripped from the delivered app
    for sid in ("a", "b", "c"):
        assert (out / "lib" / "features" / sid / f"{sid}_screen.dart").exists()
    # foundation ran serially before the screens
    assert order[0] == "t-scaffold"
    assert order[1] == "t-lib"
    assert set(order[2:]) == {"t-a", "t-b", "t-c"}


def test_parallel_merge_drops_edits_outside_owned_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rp = _make_paths(tmp_path)
    order: list[str] = []
    # screen "a" also rewrites the shared main.dart — must NOT survive the merge
    monkeypatch.setattr(claude_gen.subprocess, "run", _fake_runner(order, escape="a"))

    out = claude_gen.generate_from_tasks(rp, max_parallel=3)

    assert "hijacked" not in (out / "lib" / "main.dart").read_text()
    assert (out / "lib" / "features" / "a" / "a_screen.dart").exists()


def test_parallel_strict_raises_on_worker_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rp = _make_paths(tmp_path)

    def _run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if not cmd or cmd[0] != claude_gen.CLAUDE_BIN:
            return _REAL_RUN(cmd, **kwargs)
        cwd = Path(kwargs["cwd"])
        prompt = cmd[2]
        ttype = _field(prompt, "type")
        tid = _field(prompt, "id")
        app = cwd / "flutter_app"
        if ttype in ("scaffold", "component_library"):
            (app / "lib").mkdir(parents=True, exist_ok=True)
            (app / "pubspec.yaml").write_text("name: todo\n")
            (app / "lib" / "main.dart").write_text("void main() {}\n")
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if tid == "t-b":  # one screen worker fails permanently
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="boom")
        for sid in ("a", "c"):
            if tid.endswith(sid):
                feat = app / "lib" / "features" / sid
                feat.mkdir(parents=True, exist_ok=True)
                (feat / f"{sid}_screen.dart").write_text(f"// {sid}\n")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(claude_gen.subprocess, "run", _run)

    with pytest.raises(RuntimeError, match="screen tasks failed"):
        claude_gen.generate_from_tasks(rp, max_parallel=3, strict=True)


def test_slug_is_collision_free_for_similar_ids() -> None:
    # ids that map to the same readable prefix must still get distinct names
    assert claude_gen._slug("a.b") != claude_gen._slug("a-b")


def test_owned_paths_use_raw_screen_ids() -> None:
    owned = claude_gen._owned_paths({"id": "t-x", "screens": ["0000", "0001"]})
    assert "lib/features/0000" in owned
    assert "lib/features/0001" in owned
    assert "assets" in owned


def test_parallel_on_task_done_fires_per_screen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rp = _make_paths(tmp_path)
    order: list[str] = []
    monkeypatch.setattr(claude_gen.subprocess, "run", _fake_runner(order))
    finished: list[str] = []

    claude_gen.generate_from_tasks(rp, max_parallel=3, on_task_done=finished.append)

    assert set(finished) == {"t-scaffold", "t-lib", "t-a", "t-b", "t-c"}
