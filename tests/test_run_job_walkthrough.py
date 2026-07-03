"""Gate test for the walkthrough-only pipeline mode (PIPELINE_STOP_AFTER_WALKTHROUGH).

``run_job`` itself is live-infra-gated (real DB + MinIO + emulator + Claude CLI), so
this test exercises only the early-return gate by faking the session/storage and
monkeypatching the MVP stage functions — the same style as ``tests/test_mvp.py``.

It asserts that with ``pipeline_stop_after_walkthrough=True``:
* the codegen stage functions are NOT called,
* a ``WalkthroughResult`` is produced,
* the Job ends in ``DONE``.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest

from iosforge.common.config import Settings
from iosforge.common.types import JobState
from iosforge.db.models import ApkArtifact, Job, WalkthroughResult
from iosforge.mvp import analyze, claude_gen, compliance, crawl, emulator, screen_filter
from iosforge.mvp.paths import RunPaths
from iosforge.worker import run_job as run_job_module


class _FakeSession:
    """Minimal stand-in for a SQLAlchemy session covering run_job's usage."""

    def __init__(self, job: Job, apk_art: ApkArtifact) -> None:
        self._job = job
        self._apk = apk_art
        self.added: list[Any] = []

    def __enter__(self) -> _FakeSession:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def get(self, model: type, pk: object) -> object | None:
        return self._job if model is Job else None

    def scalar(self, stmt: object) -> ApkArtifact | None:
        return None if "video_artifacts" in str(stmt) else self._apk

    def add(self, obj: object) -> None:
        self.added.append(obj)

    def commit(self) -> None:
        return None

    def rollback(self) -> None:
        return None


class _FakeStorage:
    def __init__(self) -> None:
        self.put_keys: list[str] = []

    def get(self, _key: str, version_id: str | None = None) -> bytes:
        return b"PK\x03\x04"

    def put(self, key: str, _data: object, content_type: str | None = None) -> None:
        self.put_keys.append(key)


def test_walkthrough_only_finishes_done_and_skips_codegen(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = Job(source_app_ref="ios://app", state=JobState.QUEUED)
    job.id = uuid.uuid4()
    job_id = str(job.id)
    apk_art = ApkArtifact(job_id=job.id, storage_key="jobs/x/apk/app.apk")

    session = _FakeSession(job, apk_art)
    monkeypatch.setattr(run_job_module, "get_sessionmaker", lambda: lambda: session)
    monkeypatch.setattr(run_job_module, "S3ArtifactStorage", _FakeStorage)
    monkeypatch.setattr(
        run_job_module,
        "get_settings",
        lambda: Settings(pipeline_stop_after_walkthrough=True),
    )

    monkeypatch.setattr(emulator, "start_emulator", lambda avd: None)
    monkeypatch.setattr(emulator, "wait_for_boot", lambda: None)
    monkeypatch.setattr(emulator, "install_apk", lambda apk: "com.example.app")
    monkeypatch.setattr(emulator, "launch", lambda package: None)

    def _fake_walk(paths: RunPaths, package: str, max_screens: int) -> dict[str, object]:
        (paths.screens_dir / "0000.png").write_bytes(b"\x89PNG\r\n\x1a\n")
        result = {"package": package, "screens": [{"id": "0000"}]}
        paths.screens_json.write_text(json.dumps(result))
        return result

    monkeypatch.setattr(crawl, "walk", _fake_walk)

    codegen_calls: list[str] = []
    monkeypatch.setattr(analyze, "analyze", lambda *a, **k: codegen_calls.append("analyze"))
    monkeypatch.setattr(analyze, "decompose", lambda *a, **k: codegen_calls.append("decompose"))
    monkeypatch.setattr(
        claude_gen, "generate_from_tasks", lambda *a, **k: codegen_calls.append("generate")
    )
    monkeypatch.setattr(
        compliance, "refine_until_compliant", lambda *a, **k: codegen_calls.append("refine")
    )

    result = run_job_module.run_job.run(job_id)

    assert codegen_calls == []
    assert job.state is JobState.DONE
    assert any(isinstance(obj, WalkthroughResult) for obj in session.added)
    assert "walkthrough only" in result


def test_analysis_only_runs_filter_and_analyze_then_stops(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = Job(source_app_ref="ios://app", state=JobState.QUEUED)
    job.id = uuid.uuid4()
    job_id = str(job.id)
    apk_art = ApkArtifact(job_id=job.id, storage_key="jobs/x/apk/app.apk")

    session = _FakeSession(job, apk_art)
    monkeypatch.setattr(run_job_module, "get_sessionmaker", lambda: lambda: session)
    monkeypatch.setattr(run_job_module, "S3ArtifactStorage", _FakeStorage)
    monkeypatch.setattr(
        run_job_module, "get_settings", lambda: Settings(pipeline_stop_after_analyze=True)
    )

    monkeypatch.setattr(emulator, "start_emulator", lambda avd: None)
    monkeypatch.setattr(emulator, "wait_for_boot", lambda: None)
    monkeypatch.setattr(emulator, "install_apk", lambda apk: "com.example.app")
    monkeypatch.setattr(emulator, "launch", lambda package: None)

    def _fake_walk(paths: RunPaths, package: str, max_screens: int) -> dict[str, object]:
        (paths.screens_dir / "0000.png").write_bytes(b"\x89PNG\r\n\x1a\n")
        result = {"package": package, "screens": [{"id": "0000"}]}
        paths.screens_json.write_text(json.dumps(result))
        return result

    monkeypatch.setattr(crawl, "walk", _fake_walk)

    calls: list[str] = []
    monkeypatch.setattr(
        screen_filter, "filter_screens", lambda paths, **k: (calls.append("filter"), {})[1]
    )

    def _fake_analyze(paths: RunPaths, *a: object, **k: object) -> None:
        paths.app_spec_json.write_text("{}")
        paths.spec_md.write_text("# spec")
        calls.append("analyze")

    monkeypatch.setattr(analyze, "analyze", _fake_analyze)
    monkeypatch.setattr(analyze, "decompose", lambda *a, **k: calls.append("decompose"))
    monkeypatch.setattr(claude_gen, "generate_from_tasks", lambda *a, **k: calls.append("generate"))
    monkeypatch.setattr(
        compliance, "refine_until_compliant", lambda *a, **k: calls.append("refine")
    )

    result = run_job_module.run_job.run(job_id)

    assert calls == ["filter", "analyze"]
    assert job.state is JobState.DONE
    assert "analysis only" in result


def test_setting_defaults_false() -> None:
    assert Settings().pipeline_stop_after_walkthrough is False
    assert Settings().pipeline_stop_after_analyze is False
