"""Gate tests for the Phase 2 scope-gate wiring in the Celery worker.

Two layers, both with fake session/storage and monkeypatched stages (no live
infra, no ``claude`` CLI):

* :func:`scope_gate` — proposes a scope, stores it, parks the Job in
  ``NEEDS_INPUT`` with ``scope_status="proposed"`` + a version id, and records a
  ``Stage.SCOPE`` timeline row that it opens and closes.
* ``run_job`` appstore branch — with ``pipeline_scope_gate=True`` the analysis run
  hands off to ``scope_gate`` (enqueued, NOT the SwiftUI build in the same pass);
  with the flag off the prior behaviour is unchanged (covered by the existing
  ``tests/test_run_job_appstore.py`` regression suite).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from iosforge.common.config import Settings
from iosforge.common.types import JobState, Stage
from iosforge.db.models import DataArchiveArtifact, Job, StageTimeline, WalkthroughResult
from iosforge.mvp import analyze, appstore, feasibility, frida_ingest
from iosforge.mvp.appstore import AppStoreMetadata
from iosforge.mvp.paths import RunPaths
from iosforge.mvp.scope_models import FeasibilityReport, ScopeCounts, ScopeDecision, ScreenScope
from iosforge.storage.client import ArtifactRef
from iosforge.worker import run_job as run_job_module
from iosforge.worker import swiftui_build


def _proposed_scope() -> ScopeDecision:
    screens = [
        ScreenScope(screen_id="0000", name="Home", include=True, reason="core"),
        ScreenScope(screen_id="0001", name="Onboarding", include=False, reason="intro"),
    ]
    return ScopeDecision(
        status="proposed",
        scope_mode="core",
        screens=screens,
        feasibility=FeasibilityReport(overall_verdict="native", summary="ok"),
        counts=ScopeCounts.from_screens(screens),
        proposed_at=datetime.now(UTC),
    )


class _FakeSession:
    def __init__(self, job: Job, walk: WalkthroughResult | None) -> None:
        self._job = job
        self._walk = walk
        self.added: list[Any] = []
        self.commits = 0

    def __enter__(self) -> _FakeSession:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def get(self, model: type, pk: object) -> object | None:
        if model is Job:
            return self._job
        if model is StageTimeline:
            for obj in reversed(self.added):
                if isinstance(obj, StageTimeline) and obj.id == pk:
                    return obj
        return None

    def scalar(self, stmt: object) -> object | None:
        return self._walk if "walkthrough_results" in str(stmt) else None

    def add(self, obj: object) -> None:
        self.added.append(obj)

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        return None


class _FakeStorage:
    def __init__(self, *, app_spec_raises: bool = False) -> None:
        self.put_keys: list[str] = []
        self._app_spec_raises = app_spec_raises

    def get(self, key: str, version_id: str | None = None) -> bytes:
        if key.endswith("app_spec.json"):
            if self._app_spec_raises:
                raise RuntimeError("app_spec missing")
            return b"{}"
        return b"\x89PNG\r\n\x1a\n"

    def put(self, key: str, _data: object, content_type: str | None = None) -> None:
        self.put_keys.append(key)


def _walk(job_id: uuid.UUID) -> WalkthroughResult:
    return WalkthroughResult(
        job_id=job_id,
        screenshot_keys=[f"jobs/{job_id}/screenshots/0000.png"],
        screen_map={"package": "com.x", "screen_count": 1, "screens": [{"id": "0000"}]},
        provider="frida-appstore",
    )


def _wire_scope_gate(
    monkeypatch: pytest.MonkeyPatch,
    job: Job,
    walk: WalkthroughResult | None,
    storage: _FakeStorage,
) -> tuple[_FakeSession, dict[str, Any]]:
    session = _FakeSession(job, walk)
    monkeypatch.setattr(run_job_module, "get_sessionmaker", lambda: lambda: session)
    monkeypatch.setattr(run_job_module, "S3ArtifactStorage", lambda: storage)

    recorded: dict[str, Any] = {}

    def _fake_propose(paths: RunPaths, settings: object = None) -> ScopeDecision:
        recorded["proposed"] = True
        return _proposed_scope()

    def _fake_save(_storage: object, _job_id: str, scope: ScopeDecision) -> ArtifactRef:
        recorded["saved"] = scope
        return ArtifactRef(
            bucket="b", key="k", version_id="ver-9", size=1, content_type="application/json"
        )

    monkeypatch.setattr(feasibility, "propose", _fake_propose)
    monkeypatch.setattr(feasibility, "save_scope", _fake_save)
    return session, recorded


def test_scope_gate_proposes_and_parks_needs_input(monkeypatch: pytest.MonkeyPatch) -> None:
    job = Job(source_app_ref="ios://app", state=JobState.SCOPE, submission_kind="appstore")
    job.id = uuid.uuid4()
    job.source_app_metadata = {"app_id": "42"}
    session, recorded = _wire_scope_gate(monkeypatch, job, _walk(job.id), _FakeStorage())

    result = run_job_module.scope_gate.run(str(job.id))

    assert "scope proposed" in result
    assert job.state is JobState.NEEDS_INPUT
    assert recorded["proposed"] is True
    assert job.source_app_metadata["scope_status"] == "proposed"
    assert job.source_app_metadata["scope_version_id"] == "ver-9"
    assert job.source_app_metadata["app_id"] == "42"  # existing metadata preserved

    scope_rows = [o for o in session.added if isinstance(o, StageTimeline)]
    assert scope_rows and scope_rows[-1].stage is Stage.SCOPE
    assert scope_rows[-1].started_at is not None
    assert scope_rows[-1].finished_at is not None  # stage opened AND closed


def test_scope_gate_without_analysis_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    job = Job(source_app_ref="ios://app", state=JobState.SCOPE, submission_kind="appstore")
    job.id = uuid.uuid4()
    _wire_scope_gate(monkeypatch, job, None, _FakeStorage())

    result = run_job_module.scope_gate.run(str(job.id))
    assert job.state is JobState.FAILED
    assert "no analysis" in result


def test_scope_gate_missing_app_spec_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    job = Job(source_app_ref="ios://app", state=JobState.SCOPE, submission_kind="appstore")
    job.id = uuid.uuid4()
    _wire_scope_gate(monkeypatch, job, _walk(job.id), _FakeStorage(app_spec_raises=True))

    result = run_job_module.scope_gate.run(str(job.id))
    assert job.state is JobState.FAILED
    assert "app_spec unavailable" in result


# --------------------------------------------------------------------------- #
# run_job appstore branch — flag routes analysis into the scope gate
# --------------------------------------------------------------------------- #


class _FakeApptoreSession:
    def __init__(self, job: Job, archive: DataArchiveArtifact) -> None:
        self._job = job
        self._archive = archive
        self.added: list[Any] = []

    def __enter__(self) -> _FakeApptoreSession:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def get(self, model: type, pk: object) -> object | None:
        if model is Job:
            return self._job
        if model is StageTimeline:
            for obj in reversed(self.added):
                if isinstance(obj, StageTimeline) and obj.id == pk:
                    return obj
        return None

    def scalar(self, stmt: object) -> object | None:
        if "data_archive_artifacts" in str(stmt):
            return self._archive
        if "apk_artifacts" in str(stmt):
            return None
        return None

    def add(self, obj: object) -> None:
        self.added.append(obj)

    def commit(self) -> None:
        return None

    def rollback(self) -> None:
        return None


def _wire_appstore(
    monkeypatch: pytest.MonkeyPatch, job: Job, settings: Settings
) -> tuple[_FakeApptoreSession, list[str]]:
    archive = DataArchiveArtifact(job_id=job.id, storage_key="jobs/x/data_archive/a.zip")
    session = _FakeApptoreSession(job, archive)
    storage = _FakeStorage()
    monkeypatch.setattr(run_job_module, "get_sessionmaker", lambda: lambda: session)
    monkeypatch.setattr(run_job_module, "S3ArtifactStorage", lambda: storage)
    monkeypatch.setattr(run_job_module, "get_settings", lambda: settings)

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
    return session, calls


def test_run_job_scope_gate_on_routes_to_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    job = Job(
        source_app_ref="https://apps.apple.com/us/app/x/id42",
        state=JobState.QUEUED,
        submission_kind="appstore",
        source_app_metadata={"app_id": "42", "country": "us"},
    )
    job.id = uuid.uuid4()
    session, calls = _wire_appstore(monkeypatch, job, Settings(pipeline_scope_gate=True))

    enqueued: list[tuple[Any, Any]] = []
    monkeypatch.setattr(
        run_job_module.scope_gate, "apply_async", lambda *a, **k: enqueued.append((a, k))
    )
    # build_swiftui must NOT be enqueued in this pass even though auto_build defaults on.
    build_enqueued: list[Any] = []
    monkeypatch.setattr(
        swiftui_build.build_swiftui, "apply_async", lambda *a, **k: build_enqueued.append(k)
    )

    result = run_job_module.run_job.run(str(job.id))

    assert job.state is JobState.SCOPE
    assert "scope gate queued" in result
    assert enqueued and enqueued[0][1].get("queue") == "codegen"
    assert enqueued[0][1].get("args") == [str(job.id)]
    assert build_enqueued == []
    assert "analyze" in calls
    assert any(isinstance(o, WalkthroughResult) for o in session.added)


def test_run_job_scope_gate_off_keeps_prior_behaviour(monkeypatch: pytest.MonkeyPatch) -> None:
    job = Job(
        source_app_ref="https://apps.apple.com/us/app/x/id42",
        state=JobState.QUEUED,
        submission_kind="appstore",
        source_app_metadata={"app_id": "42", "country": "us"},
    )
    job.id = uuid.uuid4()
    _session, calls = _wire_appstore(
        monkeypatch, job, Settings(pipeline_scope_gate=False, auto_build_frontend=False)
    )

    scope_enqueued: list[Any] = []
    monkeypatch.setattr(
        run_job_module.scope_gate, "apply_async", lambda *a, **k: scope_enqueued.append(k)
    )

    result = run_job_module.run_job.run(str(job.id))

    assert job.state is JobState.DONE
    assert scope_enqueued == []  # no scope gate when the flag is off
    assert "analysis" in result
    assert calls == ["analyze"]


def test_scope_gate_setting_default_false() -> None:
    assert Settings().pipeline_scope_gate is False
