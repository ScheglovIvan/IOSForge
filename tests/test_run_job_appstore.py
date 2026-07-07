"""Gate test for the App Store + Frida-archive analysis branch of ``run_job``.

Follows the fake-session / monkeypatched-stage style of
``tests/test_run_job_walkthrough.py``. Asserts that an ``appstore`` submission
ingests the Frida archive into screens, merges best-effort App Store metadata,
runs Stage B analysis and finishes ``DONE`` **without** touching codegen
(development is a separate, human-triggered step).
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import pytest

from iosforge.common.config import Settings
from iosforge.common.types import JobState
from iosforge.db.models import DataArchiveArtifact, Job, WalkthroughResult
from iosforge.mvp import analyze, appstore, frida_ingest
from iosforge.mvp.appstore import AppStoreMetadata
from iosforge.mvp.paths import RunPaths
from iosforge.worker import run_job as run_job_module


class _FakeSession:
    def __init__(self, job: Job, archive: DataArchiveArtifact) -> None:
        self._job = job
        self._archive = archive
        self.added: list[Any] = []

    def __enter__(self) -> _FakeSession:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def get(self, model: type, pk: object) -> object | None:
        return self._job if model is Job else None

    def scalar(self, stmt: object) -> object | None:
        return self._archive if "data_archive_artifacts" in str(stmt) else None

    def add(self, obj: object) -> None:
        self.added.append(obj)

    def commit(self) -> None:
        return None

    def rollback(self) -> None:
        return None


class _FakeStorage:
    put_keys: list[str] = []

    def get(self, _key: str, version_id: str | None = None) -> bytes:
        return b"archive-bytes"

    def put(self, key: str, _data: object, content_type: str | None = None) -> None:
        type(self).put_keys.append(key)


def test_appstore_archive_analysis_then_done(monkeypatch: pytest.MonkeyPatch) -> None:
    job = Job(
        source_app_ref="https://apps.apple.com/us/app/x/id42",
        state=JobState.QUEUED,
        submission_kind="appstore",
        source_app_metadata={"app_id": "42", "country": "us"},
    )
    job.id = uuid.uuid4()
    archive = DataArchiveArtifact(job_id=job.id, storage_key="jobs/x/data_archive/a.zip")

    session = _FakeSession(job, archive)
    _FakeStorage.put_keys = []
    monkeypatch.setattr(run_job_module, "get_sessionmaker", lambda: lambda: session)
    monkeypatch.setattr(run_job_module, "S3ArtifactStorage", _FakeStorage)
    # analysis-only mode: no auto-chain into frontend build
    monkeypatch.setattr(run_job_module, "get_settings", lambda: Settings(auto_build_frontend=False))

    monkeypatch.setattr(
        appstore,
        "fetch_metadata",
        lambda *a, **k: AppStoreMetadata(app_id="42", country="us", track_name="Example"),
    )
    monkeypatch.setattr(appstore, "download_screenshots", lambda *a, **k: [])

    def _fake_ingest(archive_path: Path, paths: RunPaths) -> dict[str, Any]:
        (paths.screens_dir / "0000.png").write_bytes(b"\x89PNG\r\n\x1a\n")
        paths.network_index_json.write_text("{}")
        return {"package": "com.x", "screen_count": 1, "screens": [{"id": "0000"}]}

    monkeypatch.setattr(frida_ingest, "ingest_archive", _fake_ingest)

    calls: list[str] = []

    def _fake_analyze(paths: RunPaths, *a: object, **k: object) -> Path:
        paths.app_spec_json.write_text("{}")
        paths.spec_md.write_text("# spec")
        calls.append("analyze")
        return paths.app_spec_json

    monkeypatch.setattr(analyze, "analyze", _fake_analyze)
    monkeypatch.setattr(analyze, "decompose", lambda *a, **k: calls.append("decompose"))

    result = run_job_module.run_job.run(str(job.id))

    assert job.state is JobState.DONE
    assert "analysis" in result
    assert calls == ["analyze"]  # analysis only — no decompose/codegen


def test_appstore_analysis_autochains_frontend_build(monkeypatch: pytest.MonkeyPatch) -> None:
    job = Job(
        source_app_ref="https://apps.apple.com/us/app/x/id42",
        state=JobState.QUEUED,
        submission_kind="appstore",
        source_app_metadata={"app_id": "42", "country": "us"},
    )
    job.id = uuid.uuid4()
    archive = DataArchiveArtifact(job_id=job.id, storage_key="jobs/x/data_archive/a.zip")

    session = _FakeSession(job, archive)
    _FakeStorage.put_keys = []
    monkeypatch.setattr(run_job_module, "get_sessionmaker", lambda: lambda: session)
    monkeypatch.setattr(run_job_module, "S3ArtifactStorage", _FakeStorage)
    monkeypatch.setattr(
        run_job_module, "get_settings", lambda: Settings()
    )  # auto_build_frontend=True

    monkeypatch.setattr(
        appstore,
        "fetch_metadata",
        lambda *a, **k: AppStoreMetadata(app_id="42", country="us", track_name="Example"),
    )
    monkeypatch.setattr(appstore, "download_screenshots", lambda *a, **k: [])

    def _fake_ingest(archive_path: Path, paths: RunPaths) -> dict[str, Any]:
        (paths.screens_dir / "0000.png").write_bytes(b"\x89PNG\r\n\x1a\n")
        paths.network_index_json.write_text("{}")
        return {"package": "com.x", "screen_count": 1, "screens": [{"id": "0000"}]}

    monkeypatch.setattr(frida_ingest, "ingest_archive", _fake_ingest)

    def _fake_analyze(paths: RunPaths, *a: object, **k: object) -> Path:
        paths.app_spec_json.write_text("{}")
        paths.spec_md.write_text("# spec")
        return paths.app_spec_json

    monkeypatch.setattr(analyze, "analyze", _fake_analyze)

    enqueued: list[tuple[Any, Any]] = []
    monkeypatch.setattr(
        run_job_module.build_frontend,
        "apply_async",
        lambda *a, **k: enqueued.append((a, k)),
    )

    result = run_job_module.run_job.run(str(job.id))

    assert job.state is JobState.CODEGEN  # transitional — build_frontend picks up
    assert enqueued and enqueued[0][1].get("queue") == "codegen"
    assert enqueued[0][1].get("args") == [str(job.id)]
    assert "frontend build queued" in result
    assert job.source_app_metadata["track_name"] == "Example"  # metadata merged best-effort
    assert any(isinstance(o, WalkthroughResult) for o in session.added)
    assert any(k.endswith("app_spec.json") for k in _FakeStorage.put_keys)
    assert any("network_index" in k for k in _FakeStorage.put_keys)


def test_appstore_without_archive_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    job = Job(
        source_app_ref="https://apps.apple.com/us/app/x/id42",
        state=JobState.QUEUED,
        submission_kind="appstore",
        source_app_metadata={"app_id": "42", "country": "us"},
    )
    job.id = uuid.uuid4()

    class _NoArchiveSession(_FakeSession):
        def scalar(self, stmt: object) -> object | None:
            return None

    session = _NoArchiveSession(job, archive=DataArchiveArtifact())
    monkeypatch.setattr(run_job_module, "get_sessionmaker", lambda: lambda: session)
    monkeypatch.setattr(run_job_module, "S3ArtifactStorage", _FakeStorage)
    monkeypatch.setattr(run_job_module, "get_settings", lambda: Settings())

    result = run_job_module.run_job.run(str(job.id))
    assert job.state is JobState.FAILED
    assert "no data archive" in result
