"""Gate test for the human-triggered ``build_frontend`` task.

Hydrates from a completed analysis (``screen_map`` + stored app_spec/screenshots),
runs decompose + codegen only, and must NOT invoke compliance / backend. Follows
the fake-session / monkeypatched-stage style of ``tests/test_run_job_appstore.py``.
"""

from __future__ import annotations

import io
import uuid
import zipfile
from pathlib import Path
from typing import Any

import pytest

from iosforge.common.config import Settings
from iosforge.common.types import JobState, Stage
from iosforge.db.models import GenerationResult, Job, StageTimeline, WalkthroughResult
from iosforge.mvp import analyze, codegen, codemagic_integration, compliance, github_publish
from iosforge.mvp.paths import RunPaths
from iosforge.worker import run_job as run_job_module


def _minimal_zip() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("pubspec.yaml", "name: app")
    return buf.getvalue()


_SOURCES_ZIP = _minimal_zip()


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
        if model is Job:
            return self._job
        if model is StageTimeline:
            for obj in reversed(self.added):
                if isinstance(obj, StageTimeline) and obj.id == pk:
                    return obj
        return None

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
        if key.endswith("flutter_app.zip"):
            return _SOURCES_ZIP
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
    settings: Settings | None = None,
) -> tuple[_FakeSession, list[str]]:
    session = _FakeSession(job, walk, gen)
    _FakeStorage.put_keys = []
    monkeypatch.setattr(run_job_module, "get_sessionmaker", lambda: lambda: session)
    monkeypatch.setattr(run_job_module, "S3ArtifactStorage", _FakeStorage)
    monkeypatch.setattr(run_job_module, "get_settings", lambda: settings or Settings())

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
    session, calls = _wire(monkeypatch, job, walk, settings=Settings(codemagic_auto_build=False))

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


def test_build_frontend_auto_build_chains_codemagic(monkeypatch: pytest.MonkeyPatch) -> None:
    job = Job(source_app_ref="ios://app", state=JobState.DONE, submission_kind="appstore")
    job.id = uuid.uuid4()
    walk = WalkthroughResult(
        job_id=job.id,
        screenshot_keys=[f"jobs/{job.id}/screenshots/0000.png"],
        screen_map={"package": "com.x", "screen_count": 1, "screens": [{"id": "0000"}]},
        provider="frida-appstore",
    )
    session, calls = _wire(monkeypatch, job, walk, settings=Settings(codemagic_auto_build=True))

    monkeypatch.setattr(github_publish, "load_token", lambda s: "gh")
    monkeypatch.setattr(
        github_publish,
        "publish",
        lambda *a, **k: {"full_name": "o/r", "url": "https://github.com/o/r"},
    )
    monkeypatch.setattr(codemagic_integration, "load_token", lambda s: "cm")
    monkeypatch.setattr(
        codemagic_integration, "integrate", lambda *a, **k: {"application_id": "cm-app-1"}
    )
    enqueued: list[str] = []
    monkeypatch.setattr(
        run_job_module.run_codemagic_build,
        "apply_async",
        lambda *a, **k: enqueued.append(str(k.get("queue"))),
    )

    run_job_module.build_frontend.run(str(job.id))

    # auto-build enqueued a real CodeMagic build; the job is NOT marked DONE yet
    assert enqueued == ["delivery"]
    assert job.state is not JobState.DONE


def test_build_frontend_verify_failure_is_non_fatal(monkeypatch: pytest.MonkeyPatch) -> None:
    job = Job(source_app_ref="ios://app", state=JobState.DONE, submission_kind="appstore")
    job.id = uuid.uuid4()
    walk = WalkthroughResult(
        job_id=job.id,
        screenshot_keys=[f"jobs/{job.id}/screenshots/0000.png"],
        screen_map={"screens": [{"id": "0000"}]},
    )
    session, calls = _wire(
        monkeypatch, job, walk, verify_raises=True, settings=Settings(codemagic_auto_build=False)
    )

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


def test_build_frontend_verify_loop_repersists_corrected_sources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = Job(source_app_ref="ios://app", state=JobState.DONE, submission_kind="appstore")
    job.id = uuid.uuid4()
    walk = WalkthroughResult(
        job_id=job.id,
        screenshot_keys=[f"jobs/{job.id}/screenshots/0000.png"],
        screen_map={"screens": [{"id": "0000"}]},
    )
    settings = Settings(
        pipeline_web_verify_loop=True, web_verify_hard_gate=True, codemagic_auto_build=False
    )
    session, calls = _wire(monkeypatch, job, walk, settings=settings)

    def _fake_refine(paths: RunPaths, **k: object) -> dict[str, object]:
        calls.append("refine_loop")
        (paths.flutter_app / "corrected.dart").write_text("// fix")
        return {
            "stop_reason": "all_closed",
            "compliance_score": 0.99,
            "status": "pass",
            "structural": {
                "ok": True,
                "missing_screens": [],
                "blank_screens": [],
                "dead_links": [],
                "missing_edges": [],
            },
            "screens": [],
        }

    monkeypatch.setattr(compliance, "refine_web_until_complete", _fake_refine)

    result = run_job_module.build_frontend.run(str(job.id))

    assert "frontend built" in result
    assert job.state is JobState.DONE
    assert "refine_loop" in calls  # structural loop ran
    assert "verify_web" not in calls  # single-pass skipped
    # sources are re-archived after the loop mutates flutter_app: once before, once after
    zips = [k for k in _FakeStorage.put_keys if k.endswith("flutter_app.zip")]
    assert len(zips) == 2
    gen = next(o for o in session.added if isinstance(o, GenerationResult))
    assert gen.compliance_score == 0.99
    assert gen.selftest_report["verify"] == "web_loop"


def test_build_frontend_without_analysis_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    job = Job(source_app_ref="ios://app", state=JobState.DONE, submission_kind="appstore")
    job.id = uuid.uuid4()
    _wire(monkeypatch, job, None)

    result = run_job_module.build_frontend.run(str(job.id))
    assert job.state is JobState.FAILED
    assert "no analysis" in result


# --------------------------------------------------------------------------- #
# reverify_web — clean pass publishes + rebuilds; gated pass does neither
# --------------------------------------------------------------------------- #


def _wire_reverify(
    monkeypatch: pytest.MonkeyPatch,
    job: Job,
    walk: WalkthroughResult,
    gen: GenerationResult,
    *,
    gated: bool,
    push_raises: bool = False,
    settings: Settings | None = None,
) -> tuple[_FakeSession, list[str]]:
    session = _FakeSession(job, walk, gen)
    _FakeStorage.put_keys = []
    monkeypatch.setattr(run_job_module, "get_sessionmaker", lambda: lambda: session)
    monkeypatch.setattr(run_job_module, "S3ArtifactStorage", _FakeStorage)
    monkeypatch.setattr(run_job_module, "get_settings", lambda: settings or Settings())

    calls: list[str] = []

    def _fake_run_web_verify(
        db: object,
        job_: Job,
        paths: RunPaths,
        settings_: object,
        storage: object,
        job_id: str,
        *,
        hard_gate: bool,
    ) -> tuple[dict[str, object], bool]:
        calls.append("run_web_verify")
        (paths.flutter_app / "corrected.dart").write_text("// fix")
        if gated:
            job_.state = JobState.NEEDS_INPUT
        report = {
            "stop_reason": "max_iterations" if gated else "all_closed",
            "compliance_score": 0.80 if gated else 0.99,
            "structural": {
                "ok": not gated,
                "missing_screens": [],
                "blank_screens": [],
                "dead_links": [],
                "missing_edges": [],
            },
            "screens": [],
        }
        return report, gated

    def _fake_push_existing(*a: object, **k: object) -> None:
        calls.append("push_existing")
        if push_raises:
            raise RuntimeError("git push rejected")

    def _fake_apply_async(*a: object, **k: object) -> None:
        calls.append(f"codemagic:{k.get('queue')}")

    monkeypatch.setattr(run_job_module, "_run_web_verify", _fake_run_web_verify)
    monkeypatch.setattr(github_publish, "load_token", lambda s: "token")
    monkeypatch.setattr(github_publish, "push_existing", _fake_push_existing)
    monkeypatch.setattr(run_job_module.run_codemagic_build, "apply_async", _fake_apply_async)
    return session, calls


def _reverify_fixtures() -> tuple[Job, WalkthroughResult, GenerationResult]:
    job = Job(source_app_ref="ios://app", state=JobState.DONE, submission_kind="appstore")
    job.id = uuid.uuid4()
    job.result_version = 1
    walk = WalkthroughResult(
        job_id=job.id,
        screenshot_keys=[f"jobs/{job.id}/screenshots/0000.png"],
        screen_map={"screens": [{"id": "0000"}]},
    )
    gen = GenerationResult(
        job_id=job.id,
        sources_key=f"jobs/{job.id}/sources/flutter_app.zip",
        github_repo_url="https://github.com/acme/clone",
        codemagic={"application_id": "cm-app-1"},
    )
    return job, walk, gen


def test_reverify_web_clean_pass_publishes_and_rebuilds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job, walk, gen = _reverify_fixtures()
    session, calls = _wire_reverify(monkeypatch, job, walk, gen, gated=False)

    result = run_job_module.reverify_web.run(str(job.id))

    assert "reverified (v2)" in result
    assert job.state is JobState.DONE
    assert calls == ["run_web_verify", "push_existing", "codemagic:delivery"]
    new_gen = next(o for o in session.added if isinstance(o, GenerationResult))
    assert new_gen.github_repo_url == "https://github.com/acme/clone"
    assert new_gen.codemagic == {"application_id": "cm-app-1"}
    # corrected sources re-uploaded
    assert any(k.endswith("flutter_app.zip") for k in _FakeStorage.put_keys)


def test_reverify_web_gated_pass_skips_publish_and_rebuild(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job, walk, gen = _reverify_fixtures()
    _session, calls = _wire_reverify(monkeypatch, job, walk, gen, gated=True)

    result = run_job_module.reverify_web.run(str(job.id))

    assert "reverified (v2)" in result
    assert job.state is JobState.NEEDS_INPUT
    assert "push_existing" not in calls
    assert not any(c.startswith("codemagic:") for c in calls)


def test_reverify_web_push_failure_skips_rebuild(monkeypatch: pytest.MonkeyPatch) -> None:
    job, walk, gen = _reverify_fixtures()
    session, calls = _wire_reverify(monkeypatch, job, walk, gen, gated=False, push_raises=True)

    result = run_job_module.reverify_web.run(str(job.id))

    assert "reverified (v2)" in result
    # push was attempted but failed → the job still completes, sources persisted
    assert "push_existing" in calls
    assert job.state is JobState.DONE
    new_gen = next(o for o in session.added if isinstance(o, GenerationResult))
    assert new_gen is not None
    # a failed push must NOT trigger a codemagic rebuild of the stale branch
    assert not any(c.startswith("codemagic:") for c in calls)
    # the GITHUB_UPLOAD stage row records the failure
    gh_rows = [
        o for o in session.added if isinstance(o, StageTimeline) and o.stage is Stage.GITHUB_UPLOAD
    ]
    assert gh_rows and gh_rows[-1].error
