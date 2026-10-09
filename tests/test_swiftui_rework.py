"""Phase 6 rework cycle: operator rounds, scope extension, task gating (toolchain mocked)."""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

import pytest

from iosforge.common.config import Settings
from iosforge.common.types import JobState
from iosforge.db.models import GenerationResult, Job, StageTimeline, XcodeBuild
from iosforge.mvp import swiftui_gen, xcode
from iosforge.mvp.paths import RunPaths
from iosforge.mvp.swiftui_prompts import rework_prompt
from iosforge.worker import swiftui_rework, swiftui_tasks
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

    def get(self, key: str, version_id: str | None = None) -> bytes:
        return self.objects[key]

    def put(self, key: str, data: bytes, content_type: str | None = None) -> None:
        self.objects[key] = data


class _Session:
    def __init__(self, job: Job, gen: GenerationResult | None = None) -> None:
        self.job, self.gen, self.added, self.rolled_back = job, gen, [], False
        self.active: XcodeBuild | None = None

    def __enter__(self) -> _Session:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def get(self, model: type, pk: object) -> Any:
        if model is Job:
            return self.job
        return next((o for o in self.added if getattr(o, "id", None) == pk), None)

    def scalar(self, stmt: Any) -> Any:
        text = str(stmt)
        if "xcode_builds" in text:
            return self.active
        return self.gen

    def add(self, obj: object) -> None:
        if getattr(obj, "id", "x") is None:
            obj.id = uuid.uuid4()  # type: ignore[attr-defined]
        self.added.append(obj)

    def commit(self) -> None:
        return None

    def rollback(self) -> None:
        self.rolled_back = True


SPEC_KEY = "jobs/{}/app_spec/app_spec.json"


def _job(state: JobState = JobState.DONE, **meta: Any) -> tuple[Job, _Storage]:
    job = Job(id=uuid.uuid4(), state=state, source_app_metadata=meta, result_version=2)
    storage = _Storage(
        {
            swiftui_tasks.sources_key(str(job.id)): b"zip",
            SPEC_KEY.format(job.id): json.dumps(FULL).encode(),
        }
    )
    return job, storage


def _wire(
    monkeypatch: pytest.MonkeyPatch, job: Job, storage: _Storage, session: _Session
) -> dict[str, Any]:
    seen: dict[str, Any] = {"queued": [], "rounds": [], "extended": [], "claims": 0}
    monkeypatch.setattr(swiftui_rework, "get_sessionmaker", lambda: lambda: session)
    monkeypatch.setattr(swiftui_rework, "S3ArtifactStorage", lambda: storage)
    monkeypatch.setattr(swiftui_rework, "get_settings", lambda: Settings())
    monkeypatch.setattr(swiftui_rework.simulator, "require_toolchain", lambda: None)
    monkeypatch.setattr(swiftui_rework.simulator, "resolve_udid", lambda c: "UDID")

    def claim(db: Any, job_id: Any, allowed: Any, state: JobState) -> bool:
        seen["claims"] += 1
        if job.state not in allowed:
            return False
        job.state = state
        return True

    monkeypatch.setattr(swiftui_rework, "claim_job", claim)

    def inputs(db: Any, j: Job, s: Any, paths: RunPaths) -> None:
        paths.app_spec_json.write_text(json.dumps(FULL))

    monkeypatch.setattr(swiftui_rework, "hydrate_inputs", inputs)
    monkeypatch.setattr(
        swiftui_rework.swiftui_gen, "scope_to", lambda p, ids: seen.update(scope=list(ids))
    )
    monkeypatch.setattr(swiftui_rework, "hydrate_sources", lambda s, j, d: None)
    monkeypatch.setattr(
        swiftui_rework.swiftui_gen, "extend", lambda p, ids, spec, **k: seen["extended"].append(ids)
    )
    monkeypatch.setattr(
        swiftui_rework.swiftui_gen, "rework", lambda p, text, **k: seen["rounds"].append(text)
    )
    monkeypatch.setattr(swiftui_rework, "job_locale", lambda j, p: "en-US")
    monkeypatch.setattr(
        swiftui_rework.compliance, "verify_ios", lambda *a, **k: {"compliance_score": 0.9}
    )
    monkeypatch.setattr(
        swiftui_rework, "queue_delivery", lambda db, j: seen["queued"].append(j) or object()
    )
    return seen


@pytest.mark.parametrize("state", [JobState.CODEGEN, JobState.DELIVERY, JobState.CANCELLED])
def test_round_refuses_running_or_cancelled_jobs(
    state: JobState, monkeypatch: pytest.MonkeyPatch
) -> None:
    job, storage = _job(state)
    seen = _wire(monkeypatch, job, storage, _Session(job))
    assert "cannot be reworked" in swiftui_rework.rework_swiftui.run(str(job.id), "fix it")
    assert job.state == state and seen["claims"] == 0


def test_round_refuses_pending_scope_and_inflight_delivery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job, storage = _job(JobState.NEEDS_INPUT, scope_status="proposed")
    _wire(monkeypatch, job, storage, _Session(job))
    assert "awaits approval" in swiftui_rework.rework_swiftui.run(str(job.id), "fix it")

    job, storage = _job(JobState.DONE)
    session = _Session(job)
    session.active = XcodeBuild(job_id=job.id, status="archiving")
    seen = _wire(monkeypatch, job, storage, session)
    assert "delivery is in flight" in swiftui_rework.rework_swiftui.run(str(job.id), "fix it")
    assert job.state == JobState.DONE and seen["claims"] == 0


def test_off_mac_round_leaves_the_job_untouched(monkeypatch: pytest.MonkeyPatch) -> None:
    job, storage = _job()
    seen = _wire(monkeypatch, job, storage, _Session(job))

    def missing() -> None:
        raise swiftui_rework.simulator.SimulatorUnavailable("needs a Mac worker")

    monkeypatch.setattr(swiftui_rework.simulator, "require_toolchain", missing)
    assert "Mac worker" in swiftui_rework.rework_swiftui.run(str(job.id), "fix it")
    assert job.state == JobState.DONE and seen["claims"] == 0 and seen["queued"] == []


def test_a_lost_claim_runs_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    job, storage = _job()
    seen = _wire(monkeypatch, job, storage, _Session(job))
    monkeypatch.setattr(swiftui_rework, "claim_job", lambda *a: False)
    assert "another round" in swiftui_rework.rework_swiftui.run(str(job.id), "fix it")
    assert seen["rounds"] == [] and seen["queued"] == []


def test_failed_round_restores_the_previous_state(monkeypatch: pytest.MonkeyPatch) -> None:
    job, storage = _job(JobState.NEEDS_INPUT)
    session = _Session(job)
    seen = _wire(monkeypatch, job, storage, session)

    def boom(*a: Any, **k: Any) -> None:
        raise RuntimeError("rework round left a non-compiling app")

    monkeypatch.setattr(swiftui_rework.swiftui_gen, "rework", boom)
    assert "rework failed" in swiftui_rework.rework_swiftui.run(str(job.id), "fix it")
    stage = next(o for o in session.added if isinstance(o, StageTimeline))
    assert job.state == JobState.NEEDS_INPUT and session.rolled_back
    assert "non-compiling" in (stage.error or "") and seen["queued"] == []
    assert job.result_version == 2


def test_extension_is_recorded_and_survives_the_next_round(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job, storage = _job()
    gen = GenerationResult(job_id=job.id, sources_key="jobs/x/sources/xcode_app.zip")
    gen.compliance_score = 0.85
    session = _Session(job, gen)
    seen = _wire(monkeypatch, job, storage, session)
    swiftui_tasks.save_built_scope(storage, str(job.id), ["0000", "0001"])  # type: ignore[arg-type]

    out = swiftui_rework.rework_swiftui.run(str(job.id), "", ["0002", "0002", "9999"])

    assert out.endswith("(v3)") and seen["extended"] == [["0002"]]
    recorded = json.loads(storage.objects[swiftui_tasks.built_scope_key(str(job.id))])
    assert recorded == {"screens": ["0000", "0001", "0002"]}
    assert gen.sources_key == f"jobs/{job.id}/sources/v3/xcode_app.zip"
    assert gen.sources_key in storage.objects
    assert gen.selftest_report["versions"] == [
        {"version": 2, "sources_key": "jobs/x/sources/xcode_app.zip", "compliance_score": 0.85}
    ]
    assert job.state == JobState.DONE and seen["queued"] == [job]

    swiftui_rework.rework_swiftui.run(str(job.id), "bigger buttons")
    assert seen["scope"] == ["0000", "0001", "0002"] and seen["rounds"] == ["bigger buttons"]
    assert job.result_version == 4 and len(gen.selftest_report["versions"]) == 2


def test_extension_with_nothing_new_is_a_no_op(monkeypatch: pytest.MonkeyPatch) -> None:
    job, storage = _job()
    seen = _wire(monkeypatch, job, storage, _Session(job))
    swiftui_tasks.save_built_scope(storage, str(job.id), ["0000", "0001", "0002"])  # type: ignore[arg-type]
    out = swiftui_rework.rework_swiftui.run(str(job.id), "", ["0001", "9999"])
    assert "nothing to rework" in out and seen["claims"] == 0 and job.result_version == 2


def test_scope_to_restores_added_screens_for_the_judge(paths: RunPaths) -> None:
    crawl = {"screens": [{"id": "0000"}, {"id": "0001"}, {"id": "0002"}]}
    paths.screens_json.write_text(json.dumps(crawl))
    paths.app_spec_json.write_text(json.dumps(FULL))
    swiftui_gen.scope_to(paths, ["0000", "0001"])
    assert [s["id"] for s in json.loads(paths.screens_json.read_text())["screens"]] == [
        "0000", "0001",
    ]  # fmt: skip
    paths.app_spec_json.write_text(json.dumps(FULL))
    swiftui_gen.scope_to(paths, ["0000", "0001", "0002"])
    assert len(json.loads(paths.screens_json.read_text())["screens"]) == 3


def test_effective_scope_prefers_the_recorded_one(monkeypatch: pytest.MonkeyPatch) -> None:
    job, storage = _job()
    monkeypatch.setattr(swiftui_tasks, "scope_ids", lambda *a: ["0000"])
    settings = Settings()
    assert swiftui_tasks.effective_scope_ids(job, storage, FULL, settings) == ["0000"]  # type: ignore[arg-type]
    storage.put(swiftui_tasks.built_scope_key(str(job.id)), b"{broken")
    assert swiftui_tasks.effective_scope_ids(job, storage, FULL, settings) == ["0000"]  # type: ignore[arg-type]
    swiftui_tasks.save_built_scope(storage, str(job.id), ["0001", "gone"])  # type: ignore[arg-type]
    assert swiftui_tasks.effective_scope_ids(job, storage, FULL, settings) == ["0001"]  # type: ignore[arg-type]


def _post(
    monkeypatch: pytest.MonkeyPatch, session: _Session, storage: _Storage, *, sources: str
) -> tuple[int, list[Any]]:
    from iosforge.admin import routes_jobs
    from iosforge.admin.session import SessionData

    sent: list[Any] = []
    monkeypatch.setattr(swiftui_rework.rework_swiftui, "apply_async", lambda **k: sent.append(k))
    session.gen = GenerationResult(job_id=session.job.id, sources_key=sources)
    user = SessionData(user_id="u", username="op", csrf_token="t")
    response = routes_jobs.jobs_extend_scope(
        None,
        session.job.id,
        "t",
        "0002, 0003",
        user,
        session,
        storage,  # type: ignore[arg-type]
    )
    return response.status_code, sent


def test_extend_route_queues_on_the_mac_queue_and_guards(monkeypatch: pytest.MonkeyPatch) -> None:
    job, storage = _job()
    session = _Session(job)
    status, sent = _post(monkeypatch, session, storage, sources="jobs/x/sources/v2/xcode_app.zip")
    assert status == 303
    assert sent == [{"args": [str(job.id), "", ["0002", "0003"]], "queue": "xcode"}]

    job.state = JobState.CODEGEN
    assert _post(monkeypatch, session, storage, sources="jobs/x/sources/xcode_app.zip") == (409, [])
    job.state = JobState.DONE
    session.active = XcodeBuild(job_id=job.id, status="queued")
    assert _post(monkeypatch, session, storage, sources="jobs/x/sources/xcode_app.zip") == (409, [])


def test_extend_route_rejects_bad_csrf(monkeypatch: pytest.MonkeyPatch) -> None:
    from iosforge.admin import routes_jobs
    from iosforge.admin.session import SessionData

    job, storage = _job()
    user = SessionData(user_id="u", username="op", csrf_token="t")
    response = routes_jobs.jobs_extend_scope(
        None,
        job.id,
        "forged",
        "0002",
        user,
        _Session(job),
        storage,  # type: ignore[arg-type]
    )
    assert response.status_code == 400
