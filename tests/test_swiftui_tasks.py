"""Native SwiftUI Celery tasks: sources round-trip and the delivery task (all mocked)."""

from __future__ import annotations

import io
import json
import uuid
import zipfile
from pathlib import Path
from typing import Any

import pytest

from iosforge.common import queue
from iosforge.common.config import Settings
from iosforge.common.types import JobState, Stage
from iosforge.db.models import Job, StageTimeline, XcodeBuild
from iosforge.mvp import ios_delivery, swiftui_gen
from iosforge.worker import swiftui_tasks

SPEC: dict[str, Any] = {
    "app_name": "Demo",
    "screens": [{"id": "0011", "name": "Home", "route": "/"}],
    "navigation": {"type": "stack", "map": []},
}


class _Storage:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def put(self, key: str, data: bytes, content_type: str | None = None) -> None:
        self.objects[key] = data

    def get(self, key: str, version_id: str | None = None) -> bytes:
        return self.objects[key]

    def exists(self, key: str) -> bool:
        return key in self.objects


class _Session:
    def __init__(self, job: Job) -> None:
        self.job, self.added = job, []
        self.rows: dict[object, object] = {}

    def __enter__(self) -> _Session:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def get(self, model: type, pk: object) -> Any:
        if model is Job:
            return self.job if pk == self.job.id else None
        return self.rows.get(pk)

    def add(self, obj: object) -> None:
        if isinstance(obj, XcodeBuild) and obj.id is None:
            obj.id = uuid.uuid4()
        self.added.append(obj)

    def commit(self) -> None:
        return None


def test_sources_round_trip(tmp_path: Path) -> None:
    storage = _Storage()
    app = tmp_path / "xcode_app"
    swiftui_gen.write_scaffold(app, SPEC, app_name="Demo", bundle_id="com.ex.d")
    (app / "Demo.xcodeproj").mkdir()
    (app / "Demo.xcodeproj" / "project.pbxproj").write_text("x")
    key = swiftui_tasks.store_sources(storage, "job-1", app)
    assert key == "jobs/job-1/sources/xcode_app.zip"
    out = tmp_path / "restored"
    swiftui_tasks.hydrate_sources(storage, "job-1", out)
    assert (out / "App/Navigation/Router.swift").exists() and not (out / "Demo.xcodeproj").exists()


def test_hydrate_refuses_path_escapes(tmp_path: Path) -> None:
    storage = _Storage()
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.writestr("../evil.txt", "x")
    storage.put(swiftui_tasks.sources_key("job-1"), buffer.getvalue())
    with pytest.raises(ValueError, match="unsafe path"):
        swiftui_tasks.hydrate_sources(storage, "job-1", tmp_path / "out")


def _wire(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, result: ios_delivery.DeliveryResult
) -> tuple[Job, _Session, _Storage]:
    job = Job(id=uuid.uuid4(), state=JobState.DONE, source_app_metadata={})
    session, storage = _Session(job), _Storage()
    app = tmp_path / "src"
    swiftui_gen.write_scaffold(app, SPEC, app_name="Demo", bundle_id="com.ex.d")
    swiftui_tasks.store_sources(storage, str(job.id), app)
    storage.put(f"jobs/{job.id}/app_spec/app_spec.json", json.dumps(SPEC).encode())
    monkeypatch.setattr(swiftui_tasks, "get_sessionmaker", lambda: lambda: session)
    monkeypatch.setattr(swiftui_tasks, "S3ArtifactStorage", lambda: storage)
    monkeypatch.setattr(swiftui_tasks, "get_settings", lambda: Settings())

    def deliver(app_dir: Path, out_dir: Path, **kw: Any) -> ios_delivery.DeliveryResult:
        assert (app_dir / "App/Monetization/Subscriptions.swift").exists()
        if result.ipa:
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / "Demo-unsigned.ipa").write_bytes(b"ipa")
            result.ipa = str(out_dir / "Demo-unsigned.ipa")
        return result

    monkeypatch.setattr(swiftui_tasks.ios_delivery, "deliver", deliver)
    return job, session, storage


def test_delivery_task_records_build_and_stage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = ios_delivery.DeliveryResult("unsigned", False, "1.0", "2610091200", ipa="x", log="ok")
    job, session, storage = _wire(monkeypatch, tmp_path, result)

    assert swiftui_tasks.run_xcode_delivery.run(str(job.id)) == f"job {job.id} delivery unsigned"

    build = next(o for o in session.added if isinstance(o, XcodeBuild))
    stage = next(o for o in session.added if isinstance(o, StageTimeline))
    assert (build.status, build.build_number, build.signed) == ("unsigned", "2610091200", False)
    assert (
        build.ipa_key == f"jobs/{job.id}/ipa/Demo-unsigned.ipa" and build.ipa_key in storage.objects
    )
    assert stage.stage == Stage.DELIVERY and stage.error is None and stage.finished_at is not None
    assert job.state == JobState.DONE


def test_delivery_failure_leaves_the_job_state_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = ios_delivery.DeliveryResult("failed", False, "1.0", "1", errors=["x: error: boom"])
    job, session, _ = _wire(monkeypatch, tmp_path, result)
    swiftui_tasks.run_xcode_delivery.run(str(job.id))
    build = next(o for o in session.added if isinstance(o, XcodeBuild))
    assert build.status == "failed" and build.message == "x: error: boom"
    assert job.state == JobState.DONE


def test_delivery_exception_is_recorded_without_touching_the_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = ios_delivery.DeliveryResult("unsigned", False, "1.0", "1")
    job, session, storage = _wire(monkeypatch, tmp_path, result)
    job.state = JobState.NEEDS_INPUT
    del storage.objects[swiftui_tasks.sources_key(str(job.id))]
    queued = XcodeBuild(id=uuid.uuid4(), job_id=job.id, status="queued")
    session.rows = {queued.id: queued}

    swiftui_tasks.run_xcode_delivery.run(str(job.id), str(queued.id))

    stage = next(o for o in session.added if isinstance(o, StageTimeline))
    assert queued.status == "failed" and queued.message and queued.finished_at is not None
    assert stage.error == queued.message
    assert not any(isinstance(o, XcodeBuild) for o in session.added)
    assert job.state == JobState.NEEDS_INPUT


def test_queue_delivery_records_a_queued_row_on_the_mac_queue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = Job(id=uuid.uuid4(), state=JobState.DONE, source_app_metadata={})
    session = _Session(job)
    sent: list[dict[str, Any]] = []
    monkeypatch.setattr(swiftui_tasks, "active_build", lambda db, job_id: None)
    monkeypatch.setattr(
        swiftui_tasks.run_xcode_delivery, "apply_async", lambda **kw: sent.append(kw)
    )

    build = swiftui_tasks.queue_delivery(session, job)  # type: ignore[arg-type]

    assert build is not None and build.status == "queued" and build in session.added
    assert sent == [{"args": [str(job.id), str(build.id)], "queue": "xcode"}]
    assert swiftui_tasks.XCODE_QUEUE not in queue.QUEUE_NAMES

    monkeypatch.setattr(swiftui_tasks, "active_build", lambda db, job_id: build)
    assert swiftui_tasks.queue_delivery(session, job) is None  # type: ignore[arg-type]
    assert len(sent) == 1


def test_failed_enqueue_does_not_leave_a_queued_row(monkeypatch: pytest.MonkeyPatch) -> None:
    job = Job(id=uuid.uuid4(), state=JobState.DONE, source_app_metadata={})
    session = _Session(job)
    monkeypatch.setattr(swiftui_tasks, "active_build", lambda db, job_id: None)

    def boom(**kw: Any) -> None:
        raise ConnectionError("broker down")

    monkeypatch.setattr(swiftui_tasks.run_xcode_delivery, "apply_async", boom)
    with pytest.raises(ConnectionError):
        swiftui_tasks.queue_delivery(session, job)  # type: ignore[arg-type]
    build = next(o for o in session.added if isinstance(o, XcodeBuild))
    assert build.status == "failed" and "broker down" in (build.message or "")


def _post(job: Job, storage: _Storage, monkeypatch: pytest.MonkeyPatch) -> tuple[int, list[Any]]:
    from iosforge.admin import routes_jobs
    from iosforge.admin.session import SessionData

    queued: list[Any] = []
    monkeypatch.setattr(
        swiftui_tasks,
        "queue_delivery",
        lambda db, j: queued.append(j) or XcodeBuild(id=uuid.uuid4()),
    )
    user = SessionData(user_id="u", username="op", csrf_token="t")
    response = routes_jobs.jobs_xcode_delivery(
        None,
        job.id,
        "t",
        user,
        _Session(job),
        storage,  # type: ignore[arg-type]
    )
    return response.status_code, queued


@pytest.mark.parametrize("state", [JobState.CODEGEN, JobState.FAILED, JobState.DELIVERY])
def test_delivery_route_refuses_unfinished_jobs(
    state: JobState, monkeypatch: pytest.MonkeyPatch
) -> None:
    job = Job(id=uuid.uuid4(), state=state, source_app_metadata={})
    storage = _Storage()
    storage.put(swiftui_tasks.sources_key(str(job.id)), b"zip")
    assert _post(job, storage, monkeypatch) == (409, [])


def test_delivery_route_needs_sources_then_queues(monkeypatch: pytest.MonkeyPatch) -> None:
    job = Job(id=uuid.uuid4(), state=JobState.NEEDS_INPUT, source_app_metadata={})
    storage = _Storage()
    assert _post(job, storage, monkeypatch) == (409, [])
    storage.put(swiftui_tasks.sources_key(str(job.id)), b"zip")
    status, queued = _post(job, storage, monkeypatch)
    assert status == 303 and queued == [job]
