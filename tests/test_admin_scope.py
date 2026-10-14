"""Tests for the SCOPE approval endpoint in the admin panel (Phase 2 scope-gate).

Exercises the state gate (only NEEDS_INPUT + scope_status="proposed" is allowed)
and the happy path (job flips to CODEGEN and build_swiftui is enqueued) by
calling the route function directly with fakes, no live infra.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from types import SimpleNamespace as NS
from typing import Any

from starlette.datastructures import FormData

from iosforge.admin import csrf, routes_jobs
from iosforge.admin.session import SessionData
from iosforge.common.types import JobState
from iosforge.mvp.scope_check import ScopeProblem
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
        self.url = NS(path="/jobs/x")

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


_UNREACHABLE = ScopeProblem(
    kind="unreachable",
    screen_ids=["0002"],
    message="Screen 'Settings' (0002) is not reachable from any tab ('Home' 0011).",
    suggest_include=["faq"],
)


def _patch_check(
    monkeypatch: Any,
    problems: Callable[[dict[str, Any], ScopeDecision | None], list[ScopeProblem]] | None = None,
    *,
    load: Callable[..., dict[str, Any]] | None = None,
) -> None:
    monkeypatch.setattr(
        "iosforge.mvp.scope_check.load_full_spec", load or (lambda *_a, **_k: {"screens": []})
    )
    monkeypatch.setattr(
        "iosforge.mvp.scope_check.navigation_problems", problems or (lambda *_a, **_k: [])
    )


def _page_context(*_a: Any, **_k: Any) -> dict[str, Any]:
    job = NS(
        id="job-1",
        source_app_ref="Dogo",
        state=NS(value="needs_input"),
        created_at=datetime(2026, 10, 15, 9, 0, 0),
        source_app_metadata={"scope_status": "proposed"},
        result_version=1,
    )
    signing = {"configured": False, "own": False, "key_name": "", "key_id": "", "issuer_id": ""}
    return {
        "job": job,
        "user": NS(username="op", csrf_token="t"),
        "gen": None,
        "build": None,
        "can_ios_build": False,
        "store_slides": [],
        "signing": signing,
        "signed_ok": False,
        "sign_error": "",
        "timeline": [],
        "codegen_tasks": [],
        "scope_status": "proposed",
        "scope": _proposed_scope(),
    }


def _call(
    monkeypatch: Any,
    job: _FakeJob | None,
    form_items: list[tuple[str, str]],
    *,
    scope_mode: str = "core",
    notes: str = "",
    problems: Callable[[dict[str, Any], ScopeDecision | None], list[ScopeProblem]] | None = None,
    load: Callable[..., dict[str, Any]] | None = None,
) -> tuple[Any, _FakeDb, list[uuid.UUID], ScopeDecision | None]:
    _patch_check(monkeypatch, problems, load=load)
    monkeypatch.setattr(routes_jobs, "_detail_context", _page_context)
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
    # Full mode: the prune becomes a no-op downstream in build_swiftui, but the
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
    import iosforge.worker.swiftui_build as worker

    job = _FakeJob(JobState.NEEDS_INPUT, {"scope_status": "proposed"})
    user = _user()
    db = _FakeDb(job)
    scope = _proposed_scope()
    _patch_check(monkeypatch)
    monkeypatch.setattr("iosforge.mvp.feasibility.load_scope", lambda *_a, **_k: scope)
    monkeypatch.setattr(
        "iosforge.mvp.feasibility.save_scope",
        lambda *_a, **_k: ArtifactRef(
            bucket="b", key="k", version_id="v2", size=1, content_type="application/json"
        ),
    )

    def _boom(*_a: Any, **_k: Any) -> None:
        raise RuntimeError("broker unreachable")

    monkeypatch.setattr(worker.build_swiftui, "apply_async", _boom)

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


def test_scope_approve_blocks_unreachable_screens(monkeypatch: Any) -> None:
    job = _FakeJob(JobState.NEEDS_INPUT, {"scope_status": "proposed"})
    resp, db, enqueued, saved = _call(
        monkeypatch, job, [("include_home", "on")], problems=lambda *_a: [_UNREACHABLE]
    )
    assert resp.status_code == 422
    assert saved is None
    assert enqueued == []
    assert not db.committed
    assert job.state == JobState.NEEDS_INPUT
    assert job.source_app_metadata == {"scope_status": "proposed"}
    body = resp.body.decode()
    assert "0002" in body
    assert "Scope cannot be approved: some screens would be unreachable" in body
    assert resp.context["scope_problems"] == [_UNREACHABLE]
    assert resp.context["scope_blocked"] is True
    rendered = resp.context["scope"]
    assert rendered.status == "proposed"
    assert {s.screen_id: s.include for s in rendered.screens} == {"home": True, "faq": False}


def test_scope_check_receives_the_edited_scope(monkeypatch: Any) -> None:
    seen: list[ScopeDecision | None] = []

    def _check(_spec: dict[str, Any], scope: ScopeDecision | None) -> list[ScopeProblem]:
        seen.append(scope.model_copy(deep=True) if scope is not None else None)
        return []

    job = _FakeJob(JobState.NEEDS_INPUT, {"scope_status": "proposed"})
    _call(
        monkeypatch,
        job,
        [("include_faq", "on")],
        scope_mode="full",
        notes="faq only",
        problems=_check,
    )
    assert len(seen) == 1 and seen[0] is not None
    checked = seen[0]
    assert checked.status == "proposed"
    assert checked.scope_mode == "full"
    assert checked.notes == "faq only"
    assert {s.screen_id: s.include for s in checked.screens} == {"home": False, "faq": True}
    assert checked.counts.included == 1


def test_scope_approve_blocks_when_the_spec_cannot_be_loaded(monkeypatch: Any) -> None:
    def _missing(*_a: Any, **_k: Any) -> dict[str, Any]:
        raise RuntimeError("NoSuchKey")

    job = _FakeJob(JobState.NEEDS_INPUT, {"scope_status": "proposed"})
    resp, db, enqueued, saved = _call(monkeypatch, job, [("include_home", "on")], load=_missing)
    assert resp.status_code == 422
    assert saved is None and enqueued == [] and not db.committed
    assert job.state == JobState.NEEDS_INPUT
    assert [p.kind for p in resp.context["scope_problems"]] == ["spec"]
    assert "could not be loaded" in resp.body.decode()


def _get_detail(monkeypatch: Any) -> Any:
    monkeypatch.setattr(routes_jobs, "_detail_context", _page_context)
    job = _FakeJob(JobState.NEEDS_INPUT, {"scope_status": "proposed"})
    return routes_jobs.job_detail(
        request=_FakeRequest(FormData([])),  # type: ignore[arg-type]
        job_id=uuid.uuid4(),
        user=_user(),
        db=_FakeDb(job),  # type: ignore[arg-type]
        storage=object(),  # type: ignore[arg-type]
    )


def test_job_detail_warns_about_a_proposal_that_would_fail(monkeypatch: Any) -> None:
    _patch_check(monkeypatch, lambda *_a: [_UNREACHABLE])
    resp = _get_detail(monkeypatch)
    assert resp.status_code == 200
    assert resp.context["scope_problems"] == [_UNREACHABLE]
    body = resp.body.decode()
    assert "The proposed selection would fail code generation:" in body
    assert "0002" in body and "suggested" in body


def test_job_detail_survives_a_failing_check(monkeypatch: Any) -> None:
    def _boom(*_a: Any, **_k: Any) -> list[ScopeProblem]:
        raise RuntimeError("bad spec")

    _patch_check(monkeypatch, _boom)
    resp = _get_detail(monkeypatch)
    assert resp.status_code == 200
    assert resp.context["scope_problems"] == []
    assert "would fail code generation" not in resp.body.decode()
