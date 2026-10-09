"""Phase 6 rework cycle: operator rounds, scope extension, task gating (toolchain mocked)."""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

import pytest

from iosforge.common.types import JobState
from iosforge.db.models import Job
from iosforge.mvp import swiftui_gen, xcode
from iosforge.mvp.paths import RunPaths
from iosforge.mvp.swiftui_prompts import rework_prompt
from iosforge.worker import swiftui_tasks
from tests.test_feasibility import _spec_three_screens

FULL: dict[str, Any] = _spec_three_screens()


@pytest.fixture
def paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> RunPaths:
    rp = RunPaths.create(tmp_path / "runs")
    rp.app_spec_json.write_text(json.dumps(FULL))
    swiftui_gen.scope_to(rp, ["0000", "0001"])
    scoped = json.loads(rp.app_spec_json.read_text())
    swiftui_gen.write_scaffold(rp.xcode_app, scoped, app_name="Demo", bundle_id="com.ex.d")
    for sid in ("0000", "0001"):
        (rp.xcode_app / f"App/Features/{sid}/Screen{sid}View.swift").write_text(
            f'struct Screen{sid}View: View {{ var body: some View {{ Text("{sid}") }} }}\n'
        )
    monkeypatch.setattr(xcode, "toolchain_available", lambda: False)
    return rp


def test_rework_round_keeps_only_model_code(
    paths: RunPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    prompts: list[str] = []

    def run(workspace: Path, prompt: str, **_: Any) -> int:
        prompts.append(prompt)
        app = workspace / "xcode_app"
        (app / "App/Features/0001/Screen0001View.swift").write_text(
            "struct Screen0001View: View { // v2\n}\n"
        )
        (app / "App/Navigation/Router.swift").write_text("broken")
        return 0

    monkeypatch.setattr(swiftui_gen.claude_gen, "run_task", run)
    runs = swiftui_gen.rework(paths, "Make the meter gauge bigger")
    assert runs[0].task == "rework" and runs[0].ignored == ["App/Navigation/Router.swift"]
    assert "Make the meter gauge bigger" in prompts[0]
    assert "// v2" in (paths.xcode_app / "App/Features/0001/Screen0001View.swift").read_text()
    assert (
        "func open(_ raw: String)" in (paths.xcode_app / "App/Navigation/Router.swift").read_text()
    )


def test_rework_refuses_a_non_compiling_result(
    paths: RunPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(swiftui_gen.claude_gen, "run_task", lambda *a, **k: 0)
    monkeypatch.setattr(
        swiftui_gen, "ensure_compiles", lambda *a, **k: (swiftui_gen.GateCheck(["x: error"]), [])
    )
    before = (paths.xcode_app / "App/Features/0001/Screen0001View.swift").read_text()
    with pytest.raises(RuntimeError, match="non-compiling"):
        swiftui_gen.rework(paths, "anything")
    assert (paths.xcode_app / "App/Features/0001/Screen0001View.swift").read_text() == before


def test_extend_adds_only_the_new_screens(paths: RunPaths, monkeypatch: pytest.MonkeyPatch) -> None:
    tasks: list[str] = []

    def run(workspace: Path, prompt: str, **_: Any) -> int:
        sid = prompt.split("TASK: implement screen `")[1].split("`")[0]
        tasks.append(sid)
        (workspace / f"xcode_app/App/Features/{sid}/Screen{sid}View.swift").write_text(
            f"struct Screen{sid}View: View {{ var body: some View {{ EmptyView() }} }}\n"
        )
        return 0

    monkeypatch.setattr(swiftui_gen.claude_gen, "run_task", run)
    runs = swiftui_gen.extend(paths, ["0002", "9999", "0001"], FULL)
    assert tasks == ["0002"] and [r.task for r in runs] == ["screen-0002"]
    spec = json.loads(paths.app_spec_json.read_text())
    assert [s["id"] for s in spec["screens"]] == ["0000", "0001", "0002"]
    assert 'case s0002 = "0002"' in (paths.xcode_app / "App/Navigation/ScreenID.swift").read_text()
    assert (
        'Text("0001")' in (paths.xcode_app / "App/Features/0001/Screen0001View.swift").read_text()
    )
    assert swiftui_gen.extend(paths, ["0002"], FULL) == []


def test_rework_prompt_forbids_new_screens() -> None:
    text = rework_prompt("Fix the paywall", prompters=[])
    assert "Fix the paywall" in text and "extend the scope instead" in text


class _Storage:
    def __init__(self, objects: dict[str, bytes]) -> None:
        self.objects = objects

    def exists(self, key: str) -> bool:
        return key in self.objects


class _Session:
    def __init__(self, job: Job) -> None:
        self.job = job

    def __enter__(self) -> _Session:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def get(self, model: type, pk: object) -> Job:
        return self.job

    def add(self, obj: object) -> None:
        return None

    def commit(self) -> None:
        return None


@pytest.mark.parametrize("state", [JobState.CODEGEN, JobState.DELIVERY, JobState.ANALYSIS])
def test_rework_task_refuses_running_jobs(state: JobState, monkeypatch: pytest.MonkeyPatch) -> None:
    job = Job(id=uuid.uuid4(), state=state, source_app_metadata={})
    storage = _Storage({swiftui_tasks.sources_key(str(job.id)): b"zip"})
    monkeypatch.setattr(swiftui_tasks, "get_sessionmaker", lambda: lambda: _Session(job))
    monkeypatch.setattr(swiftui_tasks, "S3ArtifactStorage", lambda: storage)
    assert "cannot be reworked" in swiftui_tasks.rework_swiftui.run(str(job.id), "fix it")
    assert job.state == state


def test_rework_task_fails_loudly_off_mac(monkeypatch: pytest.MonkeyPatch) -> None:
    job = Job(id=uuid.uuid4(), state=JobState.DONE, source_app_metadata={})
    storage = _Storage({swiftui_tasks.sources_key(str(job.id)): b"zip"})
    monkeypatch.setattr(swiftui_tasks, "get_sessionmaker", lambda: lambda: _Session(job))
    monkeypatch.setattr(swiftui_tasks, "S3ArtifactStorage", lambda: storage)
    enqueued: list[Any] = []
    monkeypatch.setattr(
        swiftui_tasks.run_xcode_delivery, "apply_async", lambda **k: enqueued.append(k)
    )
    result = swiftui_tasks.rework_swiftui.run(str(job.id), "fix it")
    assert "rework failed" in result and job.state == JobState.FAILED and enqueued == []


def test_rework_task_refuses_a_job_awaiting_scope_approval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    meta = {"scope_status": "proposed"}
    job = Job(id=uuid.uuid4(), state=JobState.NEEDS_INPUT, source_app_metadata=meta)
    storage = _Storage({swiftui_tasks.sources_key(str(job.id)): b"zip"})
    monkeypatch.setattr(swiftui_tasks, "get_sessionmaker", lambda: lambda: _Session(job))
    monkeypatch.setattr(swiftui_tasks, "S3ArtifactStorage", lambda: storage)
    assert "cannot be reworked" in swiftui_tasks.rework_swiftui.run(str(job.id), "fix it")
    assert job.state == JobState.NEEDS_INPUT


def test_successful_round_bumps_the_version_and_queues_delivery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    job = Job(id=uuid.uuid4(), state=JobState.DONE, source_app_metadata={}, result_version=2)
    storage = _Storage({swiftui_tasks.sources_key(str(job.id)): b"zip"})
    session = _Session(job)
    monkeypatch.setattr(swiftui_tasks, "get_sessionmaker", lambda: lambda: session)
    monkeypatch.setattr(swiftui_tasks, "S3ArtifactStorage", lambda: storage)
    monkeypatch.setattr(swiftui_tasks.simulator, "require_toolchain", lambda: None)

    def inputs(db: Any, j: Job, s: Any, p: RunPaths) -> None:
        p.app_spec_json.write_text(json.dumps(_spec_three_screens()))

    monkeypatch.setattr(swiftui_tasks, "hydrate_inputs", inputs)
    monkeypatch.setattr(swiftui_tasks, "_scope_ids", lambda *a: ["0000", "0001"])
    monkeypatch.setattr(swiftui_tasks.swiftui_gen, "scope_to", lambda p, ids: None)
    monkeypatch.setattr(swiftui_tasks, "hydrate_sources", lambda s, j, d: None)
    rounds: list[str] = []
    monkeypatch.setattr(
        swiftui_tasks.swiftui_gen, "rework", lambda p, text, **k: rounds.append(text)
    )
    monkeypatch.setattr(swiftui_tasks, "_job_locale", lambda j, p: "en-US")
    monkeypatch.setattr(
        swiftui_tasks.compliance, "verify_ios", lambda *a, **k: {"compliance_score": 0.9}
    )
    monkeypatch.setattr(swiftui_tasks, "store_sources", lambda s, j, d: "jobs/x/sources/z.zip")
    queued: list[Job] = []
    monkeypatch.setattr(swiftui_tasks, "queue_delivery", lambda db, j: queued.append(j))

    assert swiftui_tasks.rework_swiftui.run(str(job.id), "bigger buttons").endswith("(v3)")
    assert rounds == ["bigger buttons"] and job.state == JobState.DONE and queued == [job]
