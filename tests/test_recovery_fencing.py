"""Run-epoch fencing of the state tasks and the delivery status fence (DECISIONS 3b).

A message queued before an operator reset or scope re-open carries an older epoch
and must not touch the job; ``epoch=None`` (messages of the old code) still works.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import pytest

from iosforge.common.config import Settings
from iosforge.common.types import JobState
from iosforge.db.models import (
    DataArchiveArtifact,
    GenerationResult,
    Job,
    WalkthroughResult,
    XcodeBuild,
)
from iosforge.mvp import analyze, appstore, frida_ingest, ios_delivery
from iosforge.mvp.appstore import AppStoreMetadata
from iosforge.mvp.paths import RunPaths
from iosforge.worker import run_job as run_job_module
from iosforge.worker import swiftui_build, swiftui_rework, swiftui_tasks


class _Untouchable:
    """A session that only hands out the job; any other use fails the test."""

    def __init__(self, job: Job) -> None:
        self.job = job
        self.added: list[Any] = []

    def __enter__(self) -> _Untouchable:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def get(self, model: type, pk: object) -> Any:
        assert model is Job
        return self.job

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"stale task used the session: {name}")


def _job(state: JobState, epoch: int) -> Job:
    return Job(
        id=uuid.uuid4(),
        source_app_ref="x",
        state=state,
        submission_kind="appstore",
        source_app_metadata={"run_epoch": epoch, "scope_status": "approved"},
    )


def test_stale_run_job_is_a_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    job = _job(JobState.QUEUED, 2)
    monkeypatch.setattr(run_job_module, "get_sessionmaker", lambda: lambda: _Untouchable(job))
    monkeypatch.setattr(run_job_module, "S3ArtifactStorage", lambda: object())
    monkeypatch.setattr(run_job_module, "get_settings", lambda: Settings(_env_file=None))
    assert "superseded" in run_job_module.run_job.run(str(job.id), epoch=1)
    assert job.state == JobState.QUEUED


def test_stale_scope_gate_is_a_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    job = _job(JobState.SCOPE, 2)
    monkeypatch.setattr(run_job_module, "get_sessionmaker", lambda: lambda: _Untouchable(job))
    monkeypatch.setattr(run_job_module, "S3ArtifactStorage", lambda: object())
    assert "superseded" in run_job_module.scope_gate.run(str(job.id), epoch=0)
    assert job.state == JobState.SCOPE


def test_stale_build_is_a_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    job = _job(JobState.CODEGEN, 1)
    monkeypatch.setattr(swiftui_build, "get_sessionmaker", lambda: lambda: _Untouchable(job))
    monkeypatch.setattr(swiftui_build, "S3ArtifactStorage", lambda: object())
    monkeypatch.setattr(swiftui_build, "get_settings", lambda: Settings(_env_file=None))
    assert "superseded" in swiftui_build.build_swiftui.run(str(job.id), epoch=0)
    assert job.state == JobState.CODEGEN


def test_stale_rework_is_a_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    job = _job(JobState.DONE, 3)
    monkeypatch.setattr(swiftui_rework, "get_sessionmaker", lambda: lambda: _Untouchable(job))
    monkeypatch.setattr(swiftui_rework, "S3ArtifactStorage", lambda: object())
    monkeypatch.setattr(swiftui_rework, "get_settings", lambda: Settings(_env_file=None))
    assert "superseded" in swiftui_rework.rework_swiftui.run(str(job.id), "fix", None, epoch=2)
    assert job.state == JobState.DONE


def test_current_epoch_build_runs_and_a_late_reset_discards_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.test_swiftui_build import _wire

    report = {"compliance_score": 0.9, "status": "pass", "structural": {"ok": True}}
    job, session, seen = _wire(monkeypatch, report)
    assert swiftui_build.build_swiftui.run(str(job.id), epoch=0) == f"job {job.id} built"
    assert job.state == JobState.DONE

    job, session, seen = _wire(monkeypatch, report)
    plain_scalar = session.scalar

    def scalar(stmt: object) -> Any:
        if "jobs.source_app_metadata" in str(stmt):
            return {"run_epoch": 1}
        return plain_scalar(stmt)

    session.scalar = scalar  # type: ignore[method-assign]
    assert "superseded" in swiftui_build.build_swiftui.run(str(job.id), epoch=0)
    assert session.rolled_back
    assert not any(isinstance(o, GenerationResult) for o in session.added)
    assert seen["queued"] == [] and "built" not in seen


def test_enqueue_build_carries_the_epoch(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[dict[str, Any]] = []
    monkeypatch.setattr(swiftui_build.build_swiftui, "apply_async", lambda **k: sent.append(k))
    swiftui_build.enqueue_build("j", epoch=4)
    swiftui_build.enqueue_build("j")
    assert sent == [
        {"args": ["j"], "kwargs": {"epoch": 4}, "queue": "xcode"},
        {"args": ["j"], "kwargs": {}, "queue": "xcode"},
    ]


class _AppstoreSession:
    def __init__(
        self, job: Job, archive: DataArchiveArtifact, epoch_in_db: int, reads_before: int
    ) -> None:
        self.job, self.archive, self.epoch_in_db = job, archive, epoch_in_db
        self.reads_before = reads_before
        self.epoch_reads = 0
        self.added: list[Any] = []
        self.executed: list[str] = []

    def __enter__(self) -> _AppstoreSession:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def get(self, model: type, pk: object) -> Any:
        return self.job if model is Job else None

    def scalar(self, stmt: object) -> Any:
        text = str(stmt)
        if "jobs.source_app_metadata" in text:
            self.epoch_reads += 1
            if self.epoch_reads <= self.reads_before:
                return dict(self.job.source_app_metadata)
            return {"run_epoch": self.epoch_in_db}
        return self.archive if "data_archive_artifacts" in text else None

    def execute(self, stmt: object) -> None:
        self.executed.append(str(stmt))

    def add(self, obj: object) -> None:
        self.added.append(obj)

    def commit(self) -> None:
        return None

    def rollback(self) -> None:
        return None


def _appstore_run(
    monkeypatch: pytest.MonkeyPatch, *, epoch: int, epoch_in_db: int, reads_before: int = 0
) -> tuple[Job, _AppstoreSession, list[dict[str, Any]], str]:
    job = Job(
        id=uuid.uuid4(),
        source_app_ref="https://apps.apple.com/us/app/x/id42",
        state=JobState.QUEUED,
        submission_kind="appstore",
        source_app_metadata={"app_id": "42", "country": "us", "run_epoch": epoch},
    )
    archive = DataArchiveArtifact(job_id=job.id, storage_key="jobs/x/data_archive/a.zip")
    session = _AppstoreSession(job, archive, epoch_in_db, reads_before)

    class _Storage:
        def get(self, _key: str, version_id: str | None = None) -> bytes:
            return b"archive"

        def put(self, key: str, _data: object, content_type: str | None = None) -> None:
            return None

    monkeypatch.setattr(run_job_module, "get_sessionmaker", lambda: lambda: session)
    monkeypatch.setattr(run_job_module, "S3ArtifactStorage", _Storage)
    monkeypatch.setattr(
        run_job_module,
        "get_settings",
        lambda: Settings(_env_file=None, pipeline_scope_gate=True),  # type: ignore[call-arg]
    )
    monkeypatch.setattr(
        appstore, "fetch_metadata", lambda *a, **k: AppStoreMetadata(app_id="42", country="us")
    )
    monkeypatch.setattr(appstore, "download_screenshots", lambda *a, **k: [])

    def _ingest(archive_path: Path, paths: RunPaths) -> dict[str, Any]:
        return {"screens": [{"id": "0000"}]}

    monkeypatch.setattr(frida_ingest, "ingest_archive", _ingest)

    def _analyze(paths: RunPaths, *a: object, **k: object) -> Path:
        paths.app_spec_json.write_text("{}")
        return paths.app_spec_json

    monkeypatch.setattr(analyze, "analyze", _analyze)
    sent: list[dict[str, Any]] = []
    monkeypatch.setattr(run_job_module.scope_gate, "apply_async", lambda **k: sent.append(k))
    result = run_job_module.run_job.run(str(job.id), epoch=epoch)
    return job, session, sent, result


def test_run_job_replaces_the_walkthrough_and_hands_its_epoch_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job, session, sent, result = _appstore_run(monkeypatch, epoch=3, epoch_in_db=3)
    assert "scope gate queued" in result
    assert job.state == JobState.SCOPE
    assert sent == [{"args": [str(job.id)], "kwargs": {"epoch": 3}, "queue": "codegen"}]
    assert any(stmt.startswith("DELETE FROM walkthrough_results") for stmt in session.executed)


def test_run_job_superseded_before_its_first_stage_stops(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job, session, sent, result = _appstore_run(monkeypatch, epoch=3, epoch_in_db=4)
    assert "stopped: superseded" in result
    assert sent == [] and session.added == [] and session.executed == []
    assert job.state == JobState.QUEUED


def test_run_job_superseded_before_the_walkthrough_write_stops(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job, session, sent, result = _appstore_run(monkeypatch, epoch=3, epoch_in_db=4, reads_before=1)
    assert "stopped: superseded" in result
    assert sent == [] and session.executed == []
    assert not any(isinstance(o, WalkthroughResult) for o in session.added)


def test_run_job_superseded_after_the_analysis_does_not_hand_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job, _session, sent, result = _appstore_run(monkeypatch, epoch=3, epoch_in_db=4, reads_before=3)
    assert "analysis discarded: superseded" in result
    assert sent == []
    assert job.state == JobState.ANALYSIS


def test_delivery_ignores_a_build_that_is_no_longer_queued(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.test_swiftui_tasks import _wire

    result = ios_delivery.DeliveryResult("unsigned", False, "1.0", "1", ipa=None, log="ok")
    job, session, _storage = _wire(monkeypatch, tmp_path, result)
    build = XcodeBuild(id=uuid.uuid4(), job_id=job.id, status="failed", message="reset by op")
    session.rows[build.id] = build

    outcome = swiftui_tasks.run_xcode_delivery.run(str(job.id), str(build.id))

    assert "nothing to do" in outcome
    assert build.status == "failed" and build.message == "reset by op"
    assert session.added == []

    build.status = "queued"
    assert swiftui_tasks.run_xcode_delivery.run(str(job.id), str(build.id)).endswith("unsigned")
    assert build.status == "unsigned"
