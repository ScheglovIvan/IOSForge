"""Tests for the SCOPE approval endpoint in the admin panel (Phase 2 scope-gate).

Exercises the state gate (only NEEDS_INPUT + scope_status="proposed" is allowed)
and the happy path (job flips to CODEGEN and build_frontend is enqueued) by
calling the route function directly with fakes, no live infra.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from typing import Any

from starlette.datastructures import FormData

from iosforge.admin import csrf, routes_jobs
from iosforge.admin.session import SessionData
from iosforge.common.types import JobState
from iosforge.mvp.scope_models import (
    FeasibilityReport,
    ScopeCounts,
    ScopeDecision,
    ScreenScope,
)
from iosforge.storage.client import ArtifactRef


class _FakeRequest:
    def __init__(self, form: FormData) -> None:
        self._form = form

    async def form(self) -> FormData:
        return self._form


class _FakeJob:
    def __init__(self, state: JobState, meta: dict[str, Any]) -> None:
        self.state = state
        self.source_app_metadata = meta


class _FakeDb:
    def __init__(self, job: _FakeJob | None) -> None:
        self._job = job
        self.committed = False

    def get(self, _model: object, _job_id: uuid.UUID) -> _FakeJob | None:
        return self._job

    def commit(self) -> None:
        self.committed = True


def _proposed_scope() -> ScopeDecision:
    screens = [
        ScreenScope(screen_id="home", name="Home", include=True, reason="core"),
        ScreenScope(screen_id="faq", name="FAQ", include=True, reason="support"),
    ]
    return ScopeDecision(
        status="proposed",
        scope_mode="core",
        screens=screens,
        core_flows=["browse"],
        feasibility=FeasibilityReport(overall_verdict="native", summary="ok"),
        counts=ScopeCounts.from_screens(screens),
        proposed_at=datetime.now(UTC),
    )


def _user() -> SessionData:
    token = csrf.new_token()
    return SessionData(user_id="1", username="op", csrf_token=token)


def _call(
    monkeypatch: Any,
    job: _FakeJob | None,
    form_items: list[tuple[str, str]],
    *,
    scope_mode: str = "core",
    notes: str = "",
) -> tuple[Any, _FakeDb, list[uuid.UUID], ScopeDecision | None]:
    user = _user()
    db = _FakeDb(job)
    saved: dict[str, ScopeDecision] = {}
    enqueued: list[uuid.UUID] = []
    scope = _proposed_scope()

    monkeypatch.setattr(
        "iosforge.mvp.feasibility.load_scope",
        lambda *_a, **_k: scope,
    )

    def _save(_storage: object, _job_id: str, decision: ScopeDecision) -> ArtifactRef:
        saved["scope"] = decision
        return ArtifactRef(
            bucket="b", key="k", version_id="v2", size=1, content_type="application/json"
        )

    monkeypatch.setattr("iosforge.mvp.feasibility.save_scope", _save)
    monkeypatch.setattr(routes_jobs, "_enqueue_build", lambda jid: enqueued.append(jid))

    request = _FakeRequest(FormData([("csrf_token", user.csrf_token), *form_items]))
    resp = asyncio.run(
        routes_jobs.jobs_scope_approve(
            request=request,  # type: ignore[arg-type]
            job_id=uuid.uuid4(),
            csrf_token=user.csrf_token,
            scope_mode=scope_mode,
            notes=notes,
            user=user,
            db=db,  # type: ignore[arg-type]
            storage=object(),  # type: ignore[arg-type]
        )
    )
    return resp, db, enqueued, saved.get("scope")


def test_scope_approve_gates_wrong_state(monkeypatch: Any) -> None:
    job = _FakeJob(JobState.DONE, {"scope_status": "proposed"})
    resp, db, enqueued, saved = _call(monkeypatch, job, [])
    assert resp.status_code == 303
    assert job.state == JobState.DONE
    assert not db.committed
    assert enqueued == []
    assert saved is None


def test_scope_approve_gates_missing_proposed(monkeypatch: Any) -> None:
    job = _FakeJob(JobState.NEEDS_INPUT, {"scope_status": "approved"})
    resp, db, enqueued, _ = _call(monkeypatch, job, [])
    assert resp.status_code == 303
    assert job.state == JobState.NEEDS_INPUT
    assert not db.committed
    assert enqueued == []


def test_scope_approve_happy_path(monkeypatch: Any) -> None:
    job = _FakeJob(JobState.NEEDS_INPUT, {"scope_status": "proposed"})
    resp, db, enqueued, saved = _call(monkeypatch, job, [("include_home", "on")])

    assert resp.status_code == 303
    assert job.state == JobState.CODEGEN
    assert db.committed
    assert len(enqueued) == 1
    assert job.source_app_metadata["scope_status"] == "approved"
    assert job.source_app_metadata["scope_version_id"] == "v2"
    assert job.source_app_metadata["scope_approved_by"] == "op"
    assert "scope_approved_at" in job.source_app_metadata

    assert saved is not None
    assert saved.status == "approved"
    assert saved.counts.included == 1
    by_id = {s.screen_id: s.include for s in saved.screens}
    assert by_id == {"home": True, "faq": False}


def test_scope_approve_full_mode_still_advances_to_codegen(monkeypatch: Any) -> None:
    # Full mode: the prune becomes a no-op downstream in build_frontend, but the
    # endpoint must still approve the scope and move the job into CODEGEN.
    job = _FakeJob(JobState.NEEDS_INPUT, {"scope_status": "proposed"})
    resp, db, enqueued, saved = _call(
        monkeypatch, job, [("include_home", "on"), ("include_faq", "on")], scope_mode="full"
    )

    assert resp.status_code == 303
    assert job.state == JobState.CODEGEN
    assert db.committed
    assert len(enqueued) == 1
    assert saved is not None
    assert saved.scope_mode == "full"
    assert saved.status == "approved"
    assert saved.counts.included == 2  # both kept in full mode


def test_scope_approve_persists_notes(monkeypatch: Any) -> None:
    job = _FakeJob(JobState.NEEDS_INPUT, {"scope_status": "proposed"})
    _resp, _db, _enqueued, saved = _call(
        monkeypatch, job, [("include_home", "on")], notes="drop FAQ, keep Home"
    )
    assert saved is not None
    assert saved.notes == "drop FAQ, keep Home"


def test_scope_approve_commits_before_enqueue_even_if_broker_down(monkeypatch: Any) -> None:
    # Documents the actual ordering: db.commit() runs BEFORE the build is enqueued,
    # and _enqueue_build swallows broker errors. So a broker outage leaves the job
    # APPROVED + CODEGEN with no task running — it must be re-enqueued by hand. This
    # mirrors the existing _enqueue pattern in jobs_create (not a Phase-2 regression).
    import iosforge.worker.run_job as worker

    job = _FakeJob(JobState.NEEDS_INPUT, {"scope_status": "proposed"})
    user = _user()
    db = _FakeDb(job)
    scope = _proposed_scope()
    monkeypatch.setattr("iosforge.mvp.feasibility.load_scope", lambda *_a, **_k: scope)
    monkeypatch.setattr(
        "iosforge.mvp.feasibility.save_scope",
        lambda *_a, **_k: ArtifactRef(
            bucket="b", key="k", version_id="v2", size=1, content_type="application/json"
        ),
    )

    def _boom(*_a: Any, **_k: Any) -> None:
        raise RuntimeError("broker unreachable")

    monkeypatch.setattr(worker.build_frontend, "apply_async", _boom)

    request = _FakeRequest(FormData([("csrf_token", user.csrf_token), ("include_home", "on")]))
    resp = asyncio.run(
        routes_jobs.jobs_scope_approve(
            request=request,  # type: ignore[arg-type]
            job_id=uuid.uuid4(),
            csrf_token=user.csrf_token,
            scope_mode="core",
            notes="",
            user=user,
            db=db,  # type: ignore[arg-type]
            storage=object(),  # type: ignore[arg-type]
        )
    )
    # No exception surfaces; the commit already happened before the failed enqueue.
    assert resp.status_code == 303
    assert job.state == JobState.CODEGEN
    assert db.committed
    assert job.source_app_metadata["scope_status"] == "approved"


def test_scope_approve_bad_csrf_rejected(monkeypatch: Any) -> None:
    job = _FakeJob(JobState.NEEDS_INPUT, {"scope_status": "proposed"})
    user = _user()
    db = _FakeDb(job)
    request = _FakeRequest(FormData([("csrf_token", "wrong")]))
    resp = asyncio.run(
        routes_jobs.jobs_scope_approve(
            request=request,  # type: ignore[arg-type]
            job_id=uuid.uuid4(),
            csrf_token="wrong",
            scope_mode="core",
            notes="",
            user=user,
            db=db,  # type: ignore[arg-type]
            storage=object(),  # type: ignore[arg-type]
        )
    )
    assert resp.status_code == 400
    assert job.state == JobState.NEEDS_INPUT
    assert not db.committed


def test_load_scope_context_only_for_proposed_or_approved(monkeypatch: Any) -> None:
    # job_detail feeds the template through routes_jobs._load_scope: the read-only
    # approved view (and the proposed review view) resolve the scope; any other
    # status resolves to None so the panel is hidden.
    scope = _proposed_scope()
    monkeypatch.setattr("iosforge.mvp.feasibility.load_scope", lambda *_a, **_k: scope)
    jid = uuid.uuid4()
    assert routes_jobs._load_scope(object(), jid, "approved") is scope  # type: ignore[arg-type]
    assert routes_jobs._load_scope(object(), jid, "proposed") is scope  # type: ignore[arg-type]
    assert routes_jobs._load_scope(object(), jid, None) is None  # type: ignore[arg-type]
    assert routes_jobs._load_scope(object(), jid, "something") is None  # type: ignore[arg-type]


def test_load_scope_swallows_storage_error(monkeypatch: Any) -> None:
    def _boom(*_a: Any, **_k: Any) -> ScopeDecision:
        raise RuntimeError("storage down")

    monkeypatch.setattr("iosforge.mvp.feasibility.load_scope", _boom)
    # A failed load must not break job_detail — it degrades to no scope panel.
    assert routes_jobs._load_scope(object(), uuid.uuid4(), "approved") is None  # type: ignore[arg-type]
