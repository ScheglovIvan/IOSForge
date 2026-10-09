"""Phase 7 SwiftUI build task and scope resolution (toolchain, model and DB mocked)."""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace
from typing import Any

import pytest

from iosforge.common.config import Settings
from iosforge.common.types import JobState
from iosforge.db.models import GenerationResult, Job, StageTimeline, WalkthroughResult
from iosforge.mvp.paths import RunPaths
from iosforge.worker import swiftui_build, swiftui_tasks
from tests.test_feasibility import _spec_three_screens


class _Session:
    def __init__(self, job: Job, existing: GenerationResult | None = None) -> None:
        self.job, self.existing, self.added = job, existing, []
        self.walk: WalkthroughResult | None = WalkthroughResult(
            job_id=job.id, screen_map={"screens": [{"id": "0000"}]}
        )
        self.running: StageTimeline | None = None
        self.rolled_back = False

    def __enter__(self) -> _Session:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def get(self, model: type, pk: object) -> Any:
        if model is Job:
            return self.job
        return next((o for o in self.added if getattr(o, "id", None) == pk), None)

    def scalar(self, stmt: object) -> Any:
        text = str(stmt)
        if "walkthrough_results" in text:
            return self.walk
        if "stage_timeline" in text:
            return self.running
        return self.existing

    def scalars(self, stmt: object) -> list[StageTimeline]:
        return [self.running] if self.running is not None else []

    def add(self, obj: object) -> None:
        if getattr(obj, "id", "x") is None:
            obj.id = uuid.uuid4()  # type: ignore[attr-defined]
        self.added.append(obj)

    def commit(self) -> None:
        return None

    def rollback(self) -> None:
        self.rolled_back = True


def _wire(
    monkeypatch: pytest.MonkeyPatch, report: dict[str, Any], *, errors: list[str] | None = None
) -> tuple[Job, _Session, dict[str, Any]]:
    job = Job(id=uuid.uuid4(), state=JobState.CODEGEN, source_app_metadata={})
    session = _Session(job)
    seen: dict[str, Any] = {"queued": []}
    monkeypatch.setattr(swiftui_build, "get_sessionmaker", lambda: lambda: session)
    monkeypatch.setattr(swiftui_build, "S3ArtifactStorage", lambda: object())
    monkeypatch.setattr(swiftui_build, "get_settings", lambda: Settings())
    monkeypatch.setattr(swiftui_build.simulator, "require_toolchain", lambda: None)
    monkeypatch.setattr(swiftui_build.simulator, "resolve_udid", lambda c: "UDID")

    def claim(db: Any, job_id: Any, allowed: Any, state: JobState) -> bool:
        if job.state not in allowed:
            return False
        job.state = state
        return True

    monkeypatch.setattr(swiftui_build, "claim_job", claim)

    def inputs(db: Any, j: Job, s: Any, paths: RunPaths) -> None:
        paths.app_spec_json.write_text(json.dumps(_spec_three_screens()))

    monkeypatch.setattr(swiftui_build, "hydrate_inputs", inputs)
    monkeypatch.setattr(swiftui_build, "scope_ids", lambda *a: ["0000", "0001"])
    monkeypatch.setattr(
        swiftui_build.swiftui_gen, "scope_to", lambda p, ids: seen.update(scope=ids)
    )
    monkeypatch.setattr(swiftui_build, "job_locale", lambda j, p: "en-US")

    def generate(paths: RunPaths, **kw: Any) -> Any:
        seen["identity"] = (kw["app_name"], kw["bundle_id"])
        return SimpleNamespace(errors=errors or [])

    monkeypatch.setattr(swiftui_build.swiftui_gen, "generate", generate)

    def refine(paths: RunPaths, env: Any, **kw: Any) -> dict[str, Any]:
        seen["udid"] = env.udid
        return report

    monkeypatch.setattr(swiftui_build.compliance, "refine_ios_until_complete", refine)
    monkeypatch.setattr(swiftui_build, "_store_screens", lambda *a: None)
    monkeypatch.setattr(
        swiftui_build,
        "store_sources",
        lambda *a, **k: f"jobs/x/sources/v{k['version']}/xcode_app.zip",
    )
    monkeypatch.setattr(
        swiftui_build, "save_built_scope", lambda s, j, ids: seen.update(built=list(ids))
    )
    monkeypatch.setattr(swiftui_build, "queue_delivery", lambda db, j: seen["queued"].append(j))
    return job, session, seen


def test_clean_build_stores_the_result_and_queues_delivery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report = {
        "compliance_score": 0.85, "stop_reason": "all_closed", "status": "pass",
        "structural": {"ok": True},
    }  # fmt: skip
    job, session, seen = _wire(monkeypatch, report)

    assert swiftui_build.build_swiftui.run(str(job.id)) == f"job {job.id} built"

    gen = next(o for o in session.added if isinstance(o, GenerationResult))
    stage = next(o for o in session.added if isinstance(o, StageTimeline))
    assert gen.sources_key.endswith("xcode_app.zip") and gen.compliance_score == 0.85
    assert gen.selftest_report["verify"] == "ios" and stage.error is None
    assert job.state == JobState.DONE and seen["queued"] == [job]
    assert seen["scope"] == ["0000", "0001"] and seen["udid"] == "UDID"
    assert (
        seen["built"] == ["0000", "0001"] and gen.sources_key == "jobs/x/sources/v1/xcode_app.zip"
    )
    assert job.codegen_target == "swiftui"


def test_structural_gaps_park_the_job_without_delivery(monkeypatch: pytest.MonkeyPatch) -> None:
    report = {
        "compliance_score": 0.7, "stop_reason": "max_iterations", "status": "fail",
        "structural": {"ok": False, "missing_screens": [{"id": "session"}]},
    }  # fmt: skip
    job, session, seen = _wire(monkeypatch, report)
    assert "hold delivery" in swiftui_build.build_swiftui.run(str(job.id))
    assert job.state == JobState.NEEDS_INPUT and seen["queued"] == []
    assert any(isinstance(o, GenerationResult) for o in session.added)
    stage = next(o for o in session.added if isinstance(o, StageTimeline))
    assert "held for the operator" in (stage.error or "")


def test_codegen_errors_fail_the_job(monkeypatch: pytest.MonkeyPatch) -> None:
    job, session, seen = _wire(monkeypatch, {}, errors=["screen-0001: boom"])
    assert "build failed" in swiftui_build.build_swiftui.run(str(job.id))
    stage = next(o for o in session.added if isinstance(o, StageTimeline))
    assert job.state == JobState.FAILED and "boom" in (stage.error or "")
    assert session.rolled_back
    assert not any(isinstance(o, GenerationResult) for o in session.added)


def test_existing_result_is_not_rebuilt(monkeypatch: pytest.MonkeyPatch) -> None:
    job, session, _ = _wire(monkeypatch, {})
    session.existing = GenerationResult(job_id=job.id, sources_key="k")
    assert "already built" in swiftui_build.build_swiftui.run(str(job.id))
    assert session.added == [] and job.state == JobState.CODEGEN


@pytest.mark.parametrize(
    ("state", "meta", "expected"),
    [
        (JobState.NEEDS_INPUT, {}, "job is"),
        (JobState.ANALYSIS, {}, "job is"),
        (JobState.DONE, {"scope_status": "proposed"}, "awaits approval"),
    ],
)
def test_build_refuses_unbuildable_jobs(
    state: JobState, meta: dict[str, Any], expected: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    job, session, _ = _wire(monkeypatch, {})
    job.state, job.source_app_metadata = state, meta
    assert expected in swiftui_build.build_swiftui.run(str(job.id))
    assert session.added == [] and job.state == state


def test_build_refuses_without_analysis(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job, session, _ = _wire(monkeypatch, {})
    session.walk = None
    assert "no finished analysis" in swiftui_build.build_swiftui.run(str(job.id))
    session.walk = WalkthroughResult(job_id=job.id, screen_map={})
    assert "no finished analysis" in swiftui_build.build_swiftui.run(str(job.id))
    assert session.added == []


def test_failed_job_can_be_built_again(monkeypatch: pytest.MonkeyPatch) -> None:
    report = {"compliance_score": 0.85, "stop_reason": "all_closed", "structural": {"ok": True}}
    job, _, seen = _wire(monkeypatch, report)
    job.state = JobState.FAILED
    assert swiftui_build.build_swiftui.run(str(job.id)) == f"job {job.id} built"
    assert job.state == JobState.DONE and seen["queued"] == [job]


def test_off_mac_build_fails_loudly(monkeypatch: pytest.MonkeyPatch) -> None:
    job, _, _ = _wire(monkeypatch, {})

    def missing() -> None:
        raise swiftui_build.simulator.SimulatorUnavailable("needs a Mac worker")

    monkeypatch.setattr(swiftui_build.simulator, "require_toolchain", missing)
    assert "Mac worker" in swiftui_build.build_swiftui.run(str(job.id))
    assert job.state == JobState.FAILED


class _Scope:
    def __init__(self, mode: str, included: list[str]) -> None:
        self.scope_mode = mode
        self.screens = [
            type("S", (), {"screen_id": sid, "include": True})() for sid in included
        ] + [type("S", (), {"screen_id": "0002", "include": False})()]


@pytest.mark.parametrize(
    ("gate", "status", "scope", "expected"),
    [
        (False, "approved", _Scope("core", ["0000"]), ["0000", "0001", "0002"]),
        (True, "proposed", _Scope("core", ["0000"]), ["0000", "0001", "0002"]),
        (True, "approved", _Scope("full", ["0000"]), ["0000", "0001", "0002"]),
        (True, "approved", _Scope("core", ["0000", "0001"]), ["0000", "0001"]),
        (True, "approved", RuntimeError("missing"), ["0000", "0001", "0002"]),
    ],
)
def test_scope_ids_follow_the_approved_scope(
    gate: bool, status: str, scope: Any, expected: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    pinned: list[Any] = []

    def load(storage: Any, job_id: str, *, version_id: str | None = None) -> Any:
        pinned.append(version_id)
        if isinstance(scope, Exception):
            raise scope
        return scope

    monkeypatch.setattr(swiftui_tasks.feasibility, "load_scope", load)
    meta = {"scope_status": status, "scope_version_id": "v7"}
    job = Job(id=uuid.uuid4(), source_app_metadata=meta)
    spec = _spec_three_screens()
    ids = swiftui_tasks.scope_ids(job, object(), spec, Settings(pipeline_scope_gate=gate))  # type: ignore[arg-type]
    assert ids == expected
    assert pinned in ([], ["v7"])


def test_build_task_is_on_the_mac_queue() -> None:
    assert swiftui_build.build_swiftui.queue == swiftui_tasks.XCODE_QUEUE


def test_redelivered_build_closes_the_dead_run_and_builds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report = {"compliance_score": 0.85, "stop_reason": "all_closed", "structural": {"ok": True}}
    job, session, seen = _wire(monkeypatch, report)
    dead = StageTimeline(job_id=job.id, stage=swiftui_build.Stage.CODEGEN)
    session.running = dead
    assert swiftui_build.build_swiftui.run(str(job.id)) == f"job {job.id} built"
    assert dead.finished_at is not None and "restarted" in (dead.error or "")
    assert job.state == JobState.DONE and seen["queued"] == [job]


def test_admin_guard_still_refuses_while_a_build_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    job, session, _ = _wire(monkeypatch, {})
    session.running = StageTimeline(job_id=job.id)
    assert swiftui_build.build_refusal(session, job) == "a build is already running"  # type: ignore[arg-type]
    assert swiftui_build.build_refusal(session, job, check_running=False) is None  # type: ignore[arg-type]


def _save_settings(session: _Session, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    from iosforge.admin import routes_jobs
    from iosforge.admin.session import SessionData

    calls: list[str] = []
    monkeypatch.setattr(routes_jobs, "_enqueue_build", lambda job_id: calls.append("build"))
    monkeypatch.setattr(swiftui_tasks, "queue_delivery", lambda db, job: calls.append("delivery"))
    user = SessionData(user_id="u", username="op", csrf_token="t")
    response = routes_jobs.jobs_build_settings(
        None,
        session.job.id,
        "t",
        "test",
        "",
        "",
        "",
        "",
        "",
        user,
        session,  # type: ignore[arg-type]
    )
    assert response.status_code == 303
    return calls


def test_build_settings_builds_only_a_buildable_job(monkeypatch: pytest.MonkeyPatch) -> None:
    job, session, _ = _wire(monkeypatch, {})
    job.state = JobState.DONE
    assert _save_settings(session, monkeypatch) == ["build"]
    job.source_app_metadata = {"scope_status": "proposed"}
    assert _save_settings(session, monkeypatch) == []
    job.source_app_metadata, job.state = {}, JobState.ANALYSIS
    assert _save_settings(session, monkeypatch) == []


def test_build_settings_redelivers_a_generated_swiftui_app(monkeypatch: pytest.MonkeyPatch) -> None:
    job, session, _ = _wire(monkeypatch, {})
    job.state = JobState.DONE
    session.existing = GenerationResult(
        job_id=job.id, sources_key="jobs/x/sources/v1/xcode_app.zip"
    )
    assert _save_settings(session, monkeypatch) == ["delivery"]
    job.state = JobState.CODEGEN
    assert _save_settings(session, monkeypatch) == []


def test_visual_cap_without_structural_gaps_ships(monkeypatch: pytest.MonkeyPatch) -> None:
    report = {
        "compliance_score": 0.87, "stop_reason": "max_iterations", "status": "pass",
        "structural": {"ok": True, "missing_screens": [], "blank_screens": []},
    }  # fmt: skip
    job, session, seen = _wire(monkeypatch, report)
    assert swiftui_build.build_swiftui.run(str(job.id)) == f"job {job.id} built"
    stage = next(o for o in session.added if isinstance(o, StageTimeline))
    assert job.state == JobState.DONE and seen["queued"] == [job] and stage.error is None


def test_report_without_a_structural_audit_is_held(monkeypatch: pytest.MonkeyPatch) -> None:
    job, session, seen = _wire(monkeypatch, {"compliance_score": 0.9, "stop_reason": "all_closed"})
    assert "hold delivery" in swiftui_build.build_swiftui.run(str(job.id))
    assert job.state == JobState.NEEDS_INPUT and seen["queued"] == []
    stage = next(o for o in session.added if isinstance(o, StageTimeline))
    assert "no structural audit" in (stage.error or "")


def test_clone_risk_is_held_even_when_structurally_complete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report = {
        "compliance_score": 0.9, "stop_reason": "max_iterations", "status": "clone_risk",
        "structural": {"ok": True},
    }  # fmt: skip
    job, session, seen = _wire(monkeypatch, report)
    assert "hold delivery" in swiftui_build.build_swiftui.run(str(job.id))
    stage = next(o for o in session.added if isinstance(o, StageTimeline))
    assert job.state == JobState.NEEDS_INPUT and seen["queued"] == []
    assert "clone risk" in (stage.error or "")


@pytest.mark.parametrize(
    ("report", "gate", "held"),
    [
        ({"status": "below_floor", "structural": {"ok": True}}, True, False),
        ({"status": "pass", "structural": {"ok": False}}, False, False),
        ({"status": "clone_risk", "structural": {"ok": True}}, False, True),
        ({"status": "pass"}, True, True),
    ],
)
def test_hold_reason_matrix(report: dict[str, Any], gate: bool, held: bool) -> None:
    assert (swiftui_build.hold_reason(report, structural_gate=gate) is not None) is held
