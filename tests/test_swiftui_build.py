"""Phase 7 SwiftUI build task and scope resolution (toolchain, model and DB mocked)."""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace
from typing import Any

import pytest

from iosforge.common.config import Settings
from iosforge.common.types import JobState
from iosforge.db.models import GenerationResult, Job, StageTimeline
from iosforge.mvp.paths import RunPaths
from iosforge.worker import swiftui_build, swiftui_tasks
from tests.test_feasibility import _spec_three_screens


class _Session:
    def __init__(self, job: Job, existing: GenerationResult | None = None) -> None:
        self.job, self.existing, self.added = job, existing, []

    def __enter__(self) -> _Session:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def get(self, model: type, pk: object) -> Job:
        return self.job

    def scalar(self, stmt: object) -> GenerationResult | None:
        return self.existing

    def add(self, obj: object) -> None:
        self.added.append(obj)

    def commit(self) -> None:
        return None


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
    report = {"compliance_score": 0.85, "stop_reason": "all_closed", "status": "pass"}
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
    report = {"compliance_score": 0.7, "stop_reason": "max_iterations", "status": "fail"}
    job, session, seen = _wire(monkeypatch, report)
    assert "hold delivery" in swiftui_build.build_swiftui.run(str(job.id))
    assert job.state == JobState.NEEDS_INPUT and seen["queued"] == []
    assert any(isinstance(o, GenerationResult) for o in session.added)


def test_codegen_errors_fail_the_job(monkeypatch: pytest.MonkeyPatch) -> None:
    job, session, seen = _wire(monkeypatch, {}, errors=["screen-0001: boom"])
    assert "build failed" in swiftui_build.build_swiftui.run(str(job.id))
    stage = next(o for o in session.added if isinstance(o, StageTimeline))
    assert job.state == JobState.FAILED and "boom" in (stage.error or "")
    assert not any(isinstance(o, GenerationResult) for o in session.added)


def test_existing_result_is_not_rebuilt(monkeypatch: pytest.MonkeyPatch) -> None:
    job, session, _ = _wire(monkeypatch, {})
    session.existing = GenerationResult(job_id=job.id, sources_key="k")
    assert "already built" in swiftui_build.build_swiftui.run(str(job.id))
    assert session.added == [] and job.state == JobState.CODEGEN


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
