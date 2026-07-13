"""Resumable frontend codegen: checkpoint roundtrip + task-skip on resume."""

from __future__ import annotations

from pathlib import Path

from iosforge.mvp import claude_gen, codegen_checkpoint
from iosforge.mvp.paths import RunPaths


def _stub_prompt(task: dict, no_ads: bool = True, diverge_content: bool = True) -> str:
    return str(task.get("id"))


class _MemStorage:
    def __init__(self) -> None:
        self.blobs: dict[str, bytes] = {}

    def put(self, key: str, data: bytes, content_type: str | None = None) -> None:
        self.blobs[key] = data

    def get(self, key: str, version_id: str | None = None) -> bytes:
        return self.blobs[key]  # KeyError propagates like a missing object

    def delete(self, key: str) -> None:
        self.blobs.pop(key, None)


def _seed_workspace(paths: RunPaths) -> None:
    ws = paths.claude_ws / "flutter_app"
    (ws / "lib").mkdir(parents=True, exist_ok=True)
    (ws / "pubspec.yaml").write_text("name: app")
    (ws / "lib" / "main.dart").write_text("void main() {}")
    paths.tasks_json.write_text('{"tasks": [{"id": "scaffold", "type": "scaffold", "title": "s"}]}')


def test_checkpoint_save_load_roundtrip(tmp_path: Path) -> None:
    storage = _MemStorage()
    src = RunPaths.create(tmp_path / "run1")
    _seed_workspace(src)
    codegen_checkpoint.save(storage, "job1", src, {"scaffold", "state-catalog"}, work_dir=tmp_path)

    # fresh run dir → restore from the checkpoint
    dst = RunPaths.create(tmp_path / "run2")
    completed = codegen_checkpoint.load(storage, "job1", dst, work_dir=tmp_path)
    assert completed == {"scaffold", "state-catalog"}
    assert dst.tasks_json.exists()
    assert (dst.claude_ws / "flutter_app" / "pubspec.yaml").read_text() == "name: app"


def test_checkpoint_excludes_git_and_worktrees(tmp_path: Path) -> None:
    storage = _MemStorage()
    src = RunPaths.create(tmp_path / "run1")
    _seed_workspace(src)
    ws = src.claude_ws / "flutter_app"
    (ws / ".git").mkdir()
    (ws / ".git" / "HEAD").write_text("ref: refs/heads/base")
    (src.claude_ws / "wt" / "t-a").mkdir(parents=True)
    codegen_checkpoint.save(storage, "job1", src, {"scaffold"}, work_dir=tmp_path)

    dst = RunPaths.create(tmp_path / "run2")
    codegen_checkpoint.load(storage, "job1", dst, work_dir=tmp_path)
    restored = dst.claude_ws / "flutter_app"
    assert (restored / "pubspec.yaml").exists()
    assert not (restored / ".git").exists()


def test_load_returns_none_without_checkpoint(tmp_path: Path) -> None:
    dst = RunPaths.create(tmp_path / "run")
    assert codegen_checkpoint.load(_MemStorage(), "nope", dst, work_dir=tmp_path) is None


def test_clear_removes_checkpoint(tmp_path: Path) -> None:
    storage = _MemStorage()
    src = RunPaths.create(tmp_path / "run")
    _seed_workspace(src)
    codegen_checkpoint.save(storage, "job1", src, {"scaffold"}, work_dir=tmp_path)
    assert storage.blobs
    codegen_checkpoint.clear(storage, "job1")
    assert storage.blobs == {}


def test_save_is_non_fatal(tmp_path: Path) -> None:
    class _Boom:
        def put(self, *a: object, **k: object) -> None:
            raise RuntimeError("s3 down")

    src = RunPaths.create(tmp_path / "run")
    _seed_workspace(src)
    # must not raise
    codegen_checkpoint.save(_Boom(), "job1", src, {"scaffold"}, work_dir=tmp_path)


def test_generate_from_tasks_skips_completed(tmp_path: Path, monkeypatch) -> None:
    paths = RunPaths.create(tmp_path / "run")
    ws = paths.claude_ws / "flutter_app"
    (ws / "lib").mkdir(parents=True, exist_ok=True)
    (ws / "pubspec.yaml").write_text("name: app")
    (ws / "lib" / "main.dart").write_text("void main() {}")
    paths.app_spec_json.write_text("{}")
    paths.tasks_json.write_text(
        '{"tasks": ['
        '{"id": "scaffold", "type": "scaffold", "title": "s", "deps": []},'
        '{"id": "t1", "type": "screen", "title": "a", "deps": ["scaffold"]},'
        '{"id": "t2", "type": "screen", "title": "b", "deps": ["scaffold"]}]}'
    )

    ran: list[str] = []
    monkeypatch.setattr(claude_gen, "_prepare_task_workspace", lambda p: None)

    def _fake_run_task(p, prompt, timeout, tlog) -> int:  # returns the CLI code (0 = ok)
        ran.append("run")
        return 0

    monkeypatch.setattr(claude_gen, "run_task", _fake_run_task)
    monkeypatch.setattr(claude_gen, "_task_prompt", _stub_prompt)

    done_cb: list[str] = []
    claude_gen.generate_from_tasks(paths, completed={"t1"}, on_task_done=done_cb.append)

    # scaffold + t2 ran (2), t1 skipped; on_task_done for the two that ran
    assert len(ran) == 2
    assert set(done_cb) == {"scaffold", "t2"}
    assert "t1" not in done_cb


def _setup_tasks(tmp_path: Path, task_json: str) -> RunPaths:
    paths = RunPaths.create(tmp_path / "run")
    ws = paths.claude_ws / "flutter_app"
    (ws / "lib").mkdir(parents=True, exist_ok=True)
    (ws / "pubspec.yaml").write_text("name: app")
    (ws / "lib" / "main.dart").write_text("void main() {}")
    paths.app_spec_json.write_text("{}")
    paths.tasks_json.write_text(task_json)
    return paths


def test_progress_callbacks_and_retry(tmp_path, monkeypatch) -> None:
    paths = _setup_tasks(
        tmp_path,
        '{"tasks": ['
        '{"id": "scaffold", "type": "scaffold", "title": "Scaffold", "deps": []},'
        '{"id": "t1", "type": "screen", "title": "Home", "deps": ["scaffold"]}]}',
    )
    monkeypatch.setattr(claude_gen, "_prepare_task_workspace", lambda p: None)
    monkeypatch.setattr(claude_gen, "_task_prompt", _stub_prompt)

    codes = {"scaffold": [0], "t1": [1, 0]}  # t1 fails once then succeeds on retry

    def _run(p, prompt, timeout, tlog) -> int:
        return codes[prompt].pop(0)

    monkeypatch.setattr(claude_gen, "run_task", _run)

    plan: list[int] = []
    events: list[tuple[int, str, str, int]] = []
    claude_gen.generate_from_tasks(
        paths,
        on_plan=plan.append,
        on_task=lambda i, tot, k, title, status, att: events.append((i, k, status, att)),
        max_attempts=2,
        strict=True,
    )

    assert plan == [2]
    assert (0, "scaffold", "running", 1) in events
    assert (0, "scaffold", "done", 1) in events
    assert (1, "t1", "running", 1) in events
    assert (1, "t1", "retrying", 2) in events  # second attempt
    assert (1, "t1", "done", 2) in events


def test_permanent_failure_stops_pipeline(tmp_path, monkeypatch) -> None:
    paths = _setup_tasks(
        tmp_path,
        '{"tasks": ['
        '{"id": "scaffold", "type": "scaffold", "title": "S", "deps": []},'
        '{"id": "t1", "type": "screen", "title": "Home", "deps": ["scaffold"]}]}',
    )
    monkeypatch.setattr(claude_gen, "_prepare_task_workspace", lambda p: None)
    monkeypatch.setattr(claude_gen, "_task_prompt", _stub_prompt)
    monkeypatch.setattr(
        claude_gen, "run_task", lambda p, prompt, timeout, tlog: 0 if prompt == "scaffold" else 1
    )

    failed: list[tuple[int, str]] = []
    import pytest

    with pytest.raises(RuntimeError, match="failed permanently"):
        claude_gen.generate_from_tasks(
            paths,
            on_task=lambda i, tot, k, title, status, att: (
                failed.append((i, status)) if status == "failed" else None
            ),
            max_attempts=2,
            strict=True,
        )
    assert (1, "failed") in failed


def test_rework_prompt_and_runner(tmp_path, monkeypatch) -> None:
    from iosforge.mvp import claude_gen

    # prompt carries the instructions + in-place discipline
    p = claude_gen._rework_prompt("Fix the paywall button")
    assert "Fix the paywall button" in p
    assert "EDIT IT IN PLACE" in p and "flutter_app/" in p

    # runner edits the existing app in place (mocked claude), moves it back
    paths = RunPaths.create(tmp_path / "run")
    app = paths.flutter_app
    (app / "lib").mkdir(parents=True, exist_ok=True)
    (app / "pubspec.yaml").write_text("name: app")
    (app / "lib" / "main.dart").write_text("void main() {}")
    paths.app_spec_json.write_text("{}")
    monkeypatch.setattr(claude_gen, "_prepare_rework_workspace", lambda pth: None)

    def _fake_run(workspace, prompt, timeout, tlog) -> int:
        ws = workspace / "flutter_app"
        (ws / "lib").mkdir(parents=True, exist_ok=True)
        (ws / "pubspec.yaml").write_text("name: app")
        (ws / "lib" / "main.dart").write_text("void main() { /* fixed */ }")
        return 0

    monkeypatch.setattr(claude_gen, "run_task", _fake_run)
    out = claude_gen.rework(paths, "Fix the paywall button")
    assert out == paths.flutter_app
    assert "fixed" in (paths.flutter_app / "lib" / "main.dart").read_text()
