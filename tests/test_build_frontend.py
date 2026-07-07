"""Gate test for the human-triggered ``build_frontend`` task.

Hydrates from a completed analysis (``screen_map`` + stored app_spec/screenshots),
runs decompose + codegen only, and must NOT invoke compliance / backend. Follows
the fake-session / monkeypatched-stage style of ``tests/test_run_job_appstore.py``.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import pytest

from iosforge.common.config import Settings
from iosforge.common.types import JobState
from iosforge.db.models import GenerationResult, Job, WalkthroughResult
from iosforge.mvp import analyze, codegen, compliance
from iosforge.mvp.paths import RunPaths
from iosforge.worker import run_job as run_job_module


class _FakeSession:
    def __init__(
        self, job: Job, walk: WalkthroughResult | None, gen: GenerationResult | None = None
    ) -> None:
        self._job = job
        self._walk = walk
        self._gen = gen
        self.added: list[Any] = []

    def __enter__(self) -> _FakeSession:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def get(self, model: type, pk: object) -> object | None:
        return self._job if model is Job else None

    def scalar(self, stmt: object) -> object | None:
        s = str(stmt)
        if "walkthrough_results" in s:
            return self._walk
        if "generation_results" in s:
            return self._gen
        return None

    def add(self, obj: object) -> None:
        self.added.append(obj)

    def commit(self) -> None:
        return None

    def rollback(self) -> None:
        return None


class _FakeStorage:
    put_keys: list[str] = []

    def get(self, key: str, version_id: str | None = None) -> bytes:
        return b"{}" if key.endswith("app_spec.json") else b"\x89PNG\r\n\x1a\n"

    def put(self, key: str, _data: object, content_type: str | None = None) -> None:
        type(self).put_keys.append(key)


def _wire(
    monkeypatch: pytest.MonkeyPatch,
    job: Job,
    walk: WalkthroughResult | None,
    gen: GenerationResult | None = None,
    *,
    verify_raises: bool = False,
) -> tuple[_FakeSession, list[str]]:
    session = _FakeSession(job, walk, gen)
    _FakeStorage.put_keys = []
    monkeypatch.setattr(run_job_module, "get_sessionmaker", lambda: lambda: session)
    monkeypatch.setattr(run_job_module, "S3ArtifactStorage", _FakeStorage)
    monkeypatch.setattr(run_job_module, "get_settings", lambda: Settings())

    calls: list[str] = []

    def _fake_decompose(paths: RunPaths, *a: object, **k: object) -> Path:
        paths.tasks_json.write_text('{"tasks": []}')
        calls.append("decompose")
        return paths.tasks_json

    def _fake_generate(paths: RunPaths, settings: object = None, *a: object, **k: object) -> Path:
        paths.flutter_app.mkdir(parents=True, exist_ok=True)
        (paths.flutter_app / "pubspec.yaml").write_text("name: app")
        calls.append("generate")
        return paths.flutter_app

    def _fake_verify_web(paths: RunPaths, **k: object) -> dict[str, object]:
        calls.append("verify_web")
        if verify_raises:
            raise RuntimeError("flutter build web did not produce build/web/index.html")
        (paths.generated_screens_dir / "0000.png").write_bytes(b"\x89PNG\r\n\x1a\n")
        return {"compliance_score": 0.87, "status": "passed", "screens": []}

    monkeypatch.setattr(analyze, "decompose", _fake_decompose)
    monkeypatch.setattr(codegen, "generate", _fake_generate)
    monkeypatch.setattr(compliance, "verify_web", _fake_verify_web)
    return session, calls


def test_build_frontend_generates_and_skips_compliance(monkeypatch: pytest.MonkeyPatch) -> None:
    job = Job(source_app_ref="ios://app", state=JobState.DONE, submission_kind="appstore")
    job.id = uuid.uuid4()
    walk = WalkthroughResult(
        job_id=job.id,
        screenshot_keys=[f"jobs/{job.id}/screenshots/0000.png"],
        screen_map={"package": "com.x", "screen_count": 1, "screens": [{"id": "0000"}]},
        provider="frida-appstore",
    )
    session, calls = _wire(monkeypatch, job, walk)

    result = run_job_module.build_frontend.run(str(job.id))

    assert "frontend built" in result
    assert job.state is JobState.DONE
    assert calls == ["decompose", "generate", "verify_web"]  # web similarity check runs
    gen = next(o for o in session.added if isinstance(o, GenerationResult))
    assert gen.compliance_score == 0.87
    assert gen.selftest_report["verify"] == "web"
    assert gen.selftest_report["mode"] == "frontend_only"
    assert any(k.endswith("flutter_app.zip") for k in _FakeStorage.put_keys)
    assert any("generated_screenshots" in k for k in _FakeStorage.put_keys)


def test_build_frontend_verify_failure_is_non_fatal(monkeypatch: pytest.MonkeyPatch) -> None:
    job = Job(source_app_ref="ios://app", state=JobState.DONE, submission_kind="appstore")
    job.id = uuid.uuid4()
    walk = WalkthroughResult(
        job_id=job.id,
        screenshot_keys=[f"jobs/{job.id}/screenshots/0000.png"],
        screen_map={"screens": [{"id": "0000"}]},
    )
    session, calls = _wire(monkeypatch, job, walk, verify_raises=True)

    result = run_job_module.build_frontend.run(str(job.id))

    # verification failed, but the frontend is still built and the job is DONE
    assert "frontend built" in result
    assert job.state is JobState.DONE
    gen = next(o for o in session.added if isinstance(o, GenerationResult))
    assert gen.compliance_score is None
    assert gen.selftest_report["status"] == "verify_failed"
    assert any(k.endswith("flutter_app.zip") for k in _FakeStorage.put_keys)


def test_build_frontend_idempotent_skips_existing(monkeypatch: pytest.MonkeyPatch) -> None:
    job = Job(source_app_ref="ios://app", state=JobState.DONE, submission_kind="appstore")
    job.id = uuid.uuid4()
    walk = WalkthroughResult(
        job_id=job.id, screenshot_keys=[], screen_map={"screens": [{"id": "0000"}]}
    )
    existing = GenerationResult(job_id=job.id, sources_key="jobs/x/sources/flutter_app.zip")
    _session, calls = _wire(monkeypatch, job, walk, gen=existing)

    result = run_job_module.build_frontend.run(str(job.id))
    assert "already built" in result
    assert calls == []  # no re-run of decompose/codegen
    assert job.state is JobState.DONE


def test_build_frontend_without_analysis_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    job = Job(source_app_ref="ios://app", state=JobState.DONE, submission_kind="appstore")
    job.id = uuid.uuid4()
    _wire(monkeypatch, job, None)

    result = run_job_module.build_frontend.run(str(job.id))
    assert job.state is JobState.FAILED
    assert "no analysis" in result
