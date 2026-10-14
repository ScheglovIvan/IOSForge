"""Job recovery from the admin: reset stuck job and re-open scope (DECISIONS 2026-10-15, 3b).

The transition table is tested as a pure function; the actions themselves run against
a throwaway PostgreSQL schema (row lock, timeline, audit log) with an in-memory
versioned object store and a fake liveness probe, through the admin routes.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import Engine, create_engine, select, text, update
from sqlalchemy.orm import Session
from starlette.datastructures import FormData

from iosforge.admin import csrf, routes_jobs
from iosforge.admin.session import SessionData
from iosforge.common.config import Settings, get_settings
from iosforge.common.types import JobState, Stage
from iosforge.db.base import Base
from iosforge.db.models import AuditLog, GenerationResult, Job, StageTimeline, XcodeBuild
from iosforge.mvp import feasibility
from iosforge.mvp.scope_check import ScopeProblem
from iosforge.mvp.scope_models import (
    FeasibilityFinding,
    FeasibilityReport,
    ScopeCounts,
    ScopeDecision,
    ScreenScope,
)
from iosforge.orchestrator import recovery
from iosforge.orchestrator.liveness import JobTasks, TaskRef
from iosforge.orchestrator.recovery import (
    OPERATOR_EVENT_PREFIX,
    RecoveryError,
    ResetPlan,
    plan_reset,
)
from iosforge.storage.client import ArtifactRef

S = JobState


@pytest.mark.parametrize(
    ("state", "built", "held", "gate", "scope_status", "active", "expected"),
    [
        (S.QUEUED, False, False, True, None, False, ResetPlan(S.QUEUED, "run_job")),
        (S.RETRYABLE, False, False, True, None, False, ResetPlan(S.QUEUED, "run_job")),
        (S.WALKTHROUGH, False, False, True, None, False, ResetPlan(S.QUEUED, "run_job")),
        (S.ANALYSIS, False, False, False, None, False, ResetPlan(S.QUEUED, "run_job")),
        (S.SCOPE, False, False, True, None, False, ResetPlan(S.SCOPE, "scope_gate")),
        (S.CODEGEN, True, False, True, "approved", False, ResetPlan(S.DONE)),
        (S.CODEGEN, True, True, True, "approved", False, ResetPlan(S.NEEDS_INPUT)),
        (
            S.CODEGEN,
            False,
            False,
            True,
            "approved",
            False,
            ResetPlan(S.NEEDS_INPUT, reopen_scope=True),
        ),
        (S.CODEGEN, False, False, False, "approved", False, ResetPlan(S.FAILED)),
        (S.VERIFY, False, False, True, None, False, ResetPlan(S.FAILED)),
        (S.DELIVERY, False, False, True, "proposed", False, ResetPlan(S.FAILED)),
        (S.DONE, True, False, True, "approved", True, ResetPlan(S.DONE, fail_build=True)),
        (S.FAILED, False, False, True, None, True, ResetPlan(S.FAILED, fail_build=True)),
        (
            S.ANALYSIS,
            False,
            False,
            True,
            None,
            True,
            ResetPlan(S.QUEUED, "run_job", fail_build=True),
        ),
    ],
)
def test_plan_reset_table(
    state: JobState,
    built: bool,
    held: bool,
    gate: bool,
    scope_status: str | None,
    active: bool,
    expected: ResetPlan,
) -> None:
    plan = plan_reset(
        state,
        built=built,
        held=held,
        scope_gate=gate,
        scope_status=scope_status,
        active_build=active,
    )
    assert plan == expected


@pytest.mark.parametrize("state", [S.DONE, S.NEEDS_INPUT, S.FAILED, S.CANCELLED])
def test_plan_reset_refuses_an_idle_job_without_a_delivery(state: JobState) -> None:
    with pytest.raises(RecoveryError) as err:
        plan_reset(
            state, built=False, held=False, scope_gate=True, scope_status=None, active_build=False
        )
    assert err.value.code == "not_stuck"


def test_epoch_helpers() -> None:
    job = Job(source_app_ref="x", state=S.QUEUED, source_app_metadata={})
    assert recovery.current_epoch(job) == 0
    assert not recovery.is_stale(job, None)
    assert not recovery.is_stale(job, 0)
    job.source_app_metadata = {"run_epoch": 3}
    assert recovery.is_stale(job, 2) and not recovery.is_stale(job, 3)
    assert recovery.epoch_of({"run_epoch": "7"}) == 0
    assert recovery.epoch_of({"run_epoch": True}) == 0


# --- DB-backed scenarios ------------------------------------------------------------ #

try:
    import psycopg  # noqa: F401

    _HAS_DRIVER = True
except ImportError:  # pragma: no cover
    _HAS_DRIVER = False


def _db_reachable(url: str) -> bool:
    try:
        eng = create_engine(url)
        with eng.connect() as conn:
            conn.execute(text("SELECT 1"))
        eng.dispose()
        return True
    except Exception:  # pragma: no cover - infra missing
        return False


_DB_URL = get_settings().database_url
needs_db = pytest.mark.skipif(
    not (_HAS_DRIVER and _db_reachable(_DB_URL)), reason="requires live Postgres"
)

LATER = timedelta(hours=1)


@pytest.fixture()
def engine() -> Iterator[Engine]:
    schema = f"t3b_{uuid.uuid4().hex[:12]}"
    admin = create_engine(_DB_URL)
    with admin.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    admin.dispose()
    eng = create_engine(_DB_URL, execution_options={"schema_translate_map": {None: schema}})
    Base.metadata.create_all(eng)
    try:
        yield eng
    finally:
        eng.dispose()
        admin = create_engine(_DB_URL)
        with admin.begin() as conn:
            conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


class _Storage:
    """In-memory versioned object store (only what scope I/O uses)."""

    def __init__(self) -> None:
        self.versions: dict[str, list[tuple[str, bytes]]] = {}

    def put(self, key: str, data: bytes, *, content_type: str | None = None) -> ArtifactRef:
        versions = self.versions.setdefault(key, [])
        version_id = f"v{len(versions) + 1}"
        versions.append((version_id, bytes(data)))
        return ArtifactRef(
            bucket="b",
            key=key,
            version_id=version_id,
            size=len(data),
            content_type=content_type or "",
        )

    def get(self, key: str, *, version_id: str | None = None) -> bytes:
        versions = self.versions[key]
        if version_id is None:
            return versions[-1][1]
        return next(data for vid, data in versions if vid == version_id)

    def exists(self, key: str) -> bool:
        return key in self.versions


def _settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {"pipeline_scope_gate": True, "job_reset_min_idle_s": 300}
    return Settings(_env_file=None, **{**base, **overrides})  # type: ignore[call-arg]


def _approved_scope() -> ScopeDecision:
    screens = [
        ScreenScope(screen_id="home", name="Home", include=True, reason="core"),
        ScreenScope(screen_id="0002", name="Settings", include=True, reason="core"),
        ScreenScope(screen_id="faq", name="FAQ", include=False, reason="support"),
    ]
    return ScopeDecision(
        status="approved",
        scope_mode="core",
        screens=screens,
        core_flows=["browse"],
        feasibility=FeasibilityReport(
            overall_verdict="native",
            summary="ok",
            findings=[
                FeasibilityFinding(
                    capability="push",
                    verdict="native",
                    note="",
                    screens=["home"],
                    tier=2,
                    module="push",
                )
            ],
        ),
        counts=ScopeCounts.from_screens(screens),
        notes="drop FAQ for v1",
        proposed_at=datetime(2026, 10, 14, 9, 0, tzinfo=UTC),
        approved_at=datetime(2026, 10, 14, 10, 0, tzinfo=UTC),
        approved_by="anna",
    )


def _make_job(
    eng: Engine,
    storage: _Storage,
    state: JobState,
    *,
    scope_status: str | None = None,
    meta: dict[str, Any] | None = None,
    open_stage: Stage | None = None,
    built: bool = False,
    build_status: str | None = None,
) -> uuid.UUID:
    job_id = uuid.uuid4()
    data: dict[str, Any] = {"app_id": "42", **(meta or {})}
    if scope_status is not None:
        ref = feasibility.save_scope(storage, str(job_id), _approved_scope())  # type: ignore[arg-type]
        data.update(
            scope_status=scope_status,
            scope_version_id=ref.version_id,
            scope_approved_at="2026-10-14T10:00:00+00:00",
            scope_approved_by="anna",
            scope_routing_error="old routing error",
        )
    with Session(eng) as db:
        db.add(Job(id=job_id, source_app_ref="Dogo", state=state, source_app_metadata=data))
        db.flush()
        if open_stage is not None:
            db.add(
                StageTimeline(job_id=job_id, stage=open_stage, started_at=datetime.now(UTC) - LATER)
            )
        if built:
            db.add(GenerationResult(job_id=job_id, sources_key="k", selftest_report={}))
        if build_status is not None:
            db.add(
                XcodeBuild(job_id=job_id, status=build_status, started_at=datetime.now(UTC) - LATER)
            )
        db.commit()
    return job_id


def _clean(*_: str) -> JobTasks:
    return JobTasks(workers_replied=("codegen@mac", "xcode@mac"))


def _user() -> SessionData:
    return SessionData(user_id="1", username="op", csrf_token=csrf.new_token())


def _snapshot(eng: Engine, job_id: uuid.UUID) -> tuple[Any, ...]:
    with Session(eng) as db:
        job = db.get(Job, job_id)
        assert job is not None
        rows = db.scalars(select(StageTimeline).where(StageTimeline.job_id == job_id)).all()
        audits = db.scalars(select(AuditLog)).all()
        return (
            job.state,
            dict(job.source_app_metadata),
            job.updated_at,
            sorted((r.stage.value, r.error, r.finished_at is None) for r in rows),
            len(audits),
        )


def _wire_routes(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, probe: Callable[[str], JobTasks]
) -> dict[str, list[Any]]:
    calls: dict[str, list[Any]] = {"run_job": [], "scope_gate": [], "build": []}
    monkeypatch.setattr(routes_jobs, "get_settings", lambda: settings)
    monkeypatch.setattr(routes_jobs, "_task_probe", lambda: probe)
    monkeypatch.setattr(
        routes_jobs, "_enqueue", lambda jid, epoch=None: calls["run_job"].append((jid, epoch))
    )
    monkeypatch.setattr(
        routes_jobs,
        "_enqueue_scope_gate",
        lambda jid, epoch=None: calls["scope_gate"].append((jid, epoch)),
    )
    monkeypatch.setattr(
        routes_jobs, "_enqueue_build", lambda jid, epoch=None: calls["build"].append((jid, epoch))
    )
    return calls


def _reset(eng: Engine, storage: _Storage, job_id: uuid.UUID, *, token: str | None = None) -> Any:
    user = _user()
    with Session(eng) as db:
        return routes_jobs.jobs_reset(
            request=None,  # type: ignore[arg-type]
            job_id=job_id,
            csrf_token=user.csrf_token if token is None else token,
            user=user,
            db=db,
            storage=storage,  # type: ignore[arg-type]
        )


def _reopen(eng: Engine, storage: _Storage, job_id: uuid.UUID, *, token: str | None = None) -> Any:
    user = _user()
    with Session(eng) as db:
        return routes_jobs.jobs_scope_reopen(
            request=None,  # type: ignore[arg-type]
            job_id=job_id,
            csrf_token=user.csrf_token if token is None else token,
            user=user,
            db=db,
            storage=storage,  # type: ignore[arg-type]
        )


def _backdate(eng: Engine, job_id: uuid.UUID, by: timedelta = LATER) -> None:
    with eng.begin() as conn:
        conn.execute(update(Job).where(Job.id == job_id).values(updated_at=Job.updated_at - by))


@needs_db
def test_reset_refuses_while_a_task_is_live(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = _Storage()
    job_id = _make_job(engine, storage, S.ANALYSIS, open_stage=Stage.ANALYSIS)
    _backdate(engine, job_id)
    live = JobTasks(live=(TaskRef("t-1", "iosforge.run_job", "codegen@mac", "active"),))
    calls = _wire_routes(monkeypatch, _settings(), lambda _jid: live)
    before = _snapshot(engine, job_id)

    resp = _reset(engine, storage, job_id)

    assert resp.status_code == 409
    assert resp.media_type == "text/plain"
    body = resp.body.decode()
    assert body.startswith("Reset is not possible now: ")
    assert "codegen@mac" in body and body.endswith(".")
    assert _snapshot(engine, job_id) == before
    assert calls == {"run_job": [], "scope_gate": [], "build": []}


@needs_db
def test_reset_refuses_a_queued_task(engine: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
    storage = _Storage()
    job_id = _make_job(engine, storage, S.CODEGEN, scope_status="approved")
    _backdate(engine, job_id)
    queued = JobTasks(queued=(TaskRef("t-2", "iosforge.build_swiftui", "xcode", "queued"),))
    _wire_routes(monkeypatch, _settings(), lambda _jid: queued)
    before = _snapshot(engine, job_id)

    resp = _reset(engine, storage, job_id)

    assert resp.status_code == 409
    assert "waiting in queue xcode" in resp.body.decode()
    assert _snapshot(engine, job_id) == before


@needs_db
def test_reset_refuses_a_recently_active_job(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = _Storage()
    job_id = _make_job(engine, storage, S.ANALYSIS)
    probed: list[str] = []
    _wire_routes(monkeypatch, _settings(), lambda jid: probed.append(jid) or _clean())
    before = _snapshot(engine, job_id)

    resp = _reset(engine, storage, job_id)

    assert resp.status_code == 409
    assert "without activity" in resp.body.decode()
    assert probed == []
    assert _snapshot(engine, job_id) == before


@needs_db
def test_reset_refuses_a_done_job_without_a_delivery(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = _Storage()
    job_id = _make_job(engine, storage, S.DONE, built=True)
    _backdate(engine, job_id)
    _wire_routes(monkeypatch, _settings(), _clean)

    resp = _reset(engine, storage, job_id)

    assert resp.status_code == 409
    assert "not stuck" in resp.body.decode()


@needs_db
def test_reset_and_reopen_answer_404_and_400(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = _Storage()
    job_id = _make_job(engine, storage, S.ANALYSIS)
    _backdate(engine, job_id)
    _wire_routes(monkeypatch, _settings(), _clean)
    before = _snapshot(engine, job_id)

    assert _reset(engine, storage, uuid.uuid4()).status_code == 404
    assert _reopen(engine, storage, uuid.uuid4()).status_code == 404
    assert _reset(engine, storage, job_id, token="wrong").status_code == 400
    assert _reopen(engine, storage, job_id, token="wrong").status_code == 400
    assert _snapshot(engine, job_id) == before


@needs_db
def test_reset_codegen_orphan_with_approved_scope_reopens_it(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = _Storage()
    job_id = _make_job(
        engine, storage, S.CODEGEN, scope_status="approved", open_stage=Stage.CODEGEN
    )
    _backdate(engine, job_id)
    orphan = JobTasks(
        orphaned=(TaskRef("dead-1", "iosforge.build_swiftui", "xcode", "unacked"),),
        workers_replied=("xcode@mac",),
    )
    calls = _wire_routes(monkeypatch, _settings(), lambda _jid: orphan)

    resp = _reset(engine, storage, job_id)

    assert resp.status_code == 303
    assert resp.headers["location"] == f"/jobs/{job_id}?reset=needs_input#t-scope"
    assert calls == {"run_job": [], "scope_gate": [], "build": []}
    with Session(engine) as db:
        job = db.get(Job, job_id)
        assert job is not None
        meta = job.source_app_metadata
        assert job.state == S.NEEDS_INPUT
        assert meta["scope_status"] == "proposed"
        assert meta["run_epoch"] == 1
        assert meta["last_recovery"]["action"] == "reset"
        assert meta["last_recovery"]["from"] == "codegen"
        rows = db.scalars(
            select(StageTimeline)
            .where(StageTimeline.job_id == job_id)
            .order_by(StageTimeline.created_at)
        ).all()
        assert rows[0].finished_at is not None
        assert rows[0].error == "interrupted — reset by op"
        event = rows[-1]
        assert event.error is not None and event.error.startswith(OPERATOR_EVENT_PREFIX)
        assert "codegen → needs_input" in event.error and event.duration_ms == 0
        audit = db.scalars(select(AuditLog)).one()
        assert audit.actor == "op" and audit.action == "job.reset"
        assert audit.object_ref["orphaned_task_ids"] == ["dead-1"]
        assert audit.object_ref["epoch"] == 1 and audit.object_ref["to"] == "needs_input"
        assert audit.object_ref["scope_version_to"] == meta["scope_version_id"]
    latest = feasibility.load_scope(storage, str(job_id))  # type: ignore[arg-type]
    assert latest.status == "proposed" and latest.approved_by is None


@needs_db
def test_reset_analysis_requeues_run_job_after_the_commit(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = _Storage()
    job_id = _make_job(engine, storage, S.ANALYSIS, open_stage=Stage.ANALYSIS)
    _backdate(engine, job_id)
    _wire_routes(monkeypatch, _settings(), _clean)
    seen: list[tuple[JobState, int]] = []

    def _enqueue(jid: uuid.UUID, epoch: int | None = None) -> None:
        with Session(engine) as other:
            job = other.get(Job, jid)
            assert job is not None
            seen.append((job.state, job.source_app_metadata["run_epoch"]))
        assert epoch == 1

    monkeypatch.setattr(routes_jobs, "_enqueue", _enqueue)

    resp = _reset(engine, storage, job_id)

    assert resp.status_code == 303
    assert resp.headers["location"] == f"/jobs/{job_id}?reset=queued"
    assert seen == [(S.QUEUED, 1)]


@needs_db
def test_reset_scope_requeues_the_scope_gate(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = _Storage()
    job_id = _make_job(engine, storage, S.SCOPE, open_stage=Stage.SCOPE)
    _backdate(engine, job_id)
    calls = _wire_routes(monkeypatch, _settings(), _clean)

    assert _reset(engine, storage, job_id).status_code == 303
    assert calls["scope_gate"] == [(job_id, 1)]


@needs_db
def test_reset_survives_a_broker_outage_after_the_commit(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    from iosforge.worker import run_job as run_job_module

    storage = _Storage()
    job_id = _make_job(engine, storage, S.WALKTHROUGH)
    _backdate(engine, job_id)
    monkeypatch.setattr(routes_jobs, "get_settings", _settings)
    monkeypatch.setattr(routes_jobs, "_task_probe", lambda: _clean)

    def _boom(*_a: Any, **_k: Any) -> None:
        raise RuntimeError("broker down")

    monkeypatch.setattr(run_job_module.run_job, "apply_async", _boom)

    assert _reset(engine, storage, job_id).status_code == 303
    with Session(engine) as db:
        job = db.get(Job, job_id)
        assert job is not None and job.state == S.QUEUED


@needs_db
def test_reset_fails_a_stuck_native_delivery(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = _Storage()
    job_id = _make_job(
        engine, storage, S.DONE, built=True, build_status="archiving", open_stage=Stage.DELIVERY
    )
    _backdate(engine, job_id)
    with engine.begin() as conn:
        conn.execute(update(XcodeBuild).values(updated_at=XcodeBuild.updated_at - LATER))
    _wire_routes(monkeypatch, _settings(), _clean)

    resp = _reset(engine, storage, job_id)

    assert resp.status_code == 303
    assert resp.headers["location"] == f"/jobs/{job_id}?reset=done"
    with Session(engine) as db:
        job = db.get(Job, job_id)
        build = db.scalars(select(XcodeBuild)).one()
        assert job is not None and job.state == S.DONE
        assert job.source_app_metadata["run_epoch"] == 1
        assert build.status == "failed" and build.finished_at is not None
        assert build.message is not None and "reset by op" in build.message
        stages = db.scalars(select(StageTimeline).where(StageTimeline.stage == Stage.DELIVERY))
        assert all(row.finished_at is not None for row in stages)


@needs_db
def test_reset_refuses_when_the_job_moves_during_the_probe(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = _Storage()
    job_id = _make_job(engine, storage, S.ANALYSIS)
    _backdate(engine, job_id)

    def _racing_probe(_jid: str) -> JobTasks:
        with Session(engine) as other:
            job = other.get(Job, job_id)
            assert job is not None
            job.state = S.SCOPE
            other.commit()
        return _clean()

    calls = _wire_routes(monkeypatch, _settings(), _racing_probe)

    resp = _reset(engine, storage, job_id)

    assert resp.status_code == 409
    assert "moved on" in resp.body.decode()
    assert calls["run_job"] == []
    with Session(engine) as db:
        job = db.get(Job, job_id)
        assert job is not None and job.state == S.SCOPE
        assert "run_epoch" not in job.source_app_metadata
        assert db.scalars(select(AuditLog)).all() == []


@needs_db
def test_reset_refuses_when_the_broker_is_unverifiable(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = _Storage()
    job_id = _make_job(engine, storage, S.ANALYSIS)
    _backdate(engine, job_id)

    def _down(_jid: str) -> JobTasks:
        raise RecoveryError("unverifiable", "the task queue cannot be checked (redis down)")

    _wire_routes(monkeypatch, _settings(), _down)
    before = _snapshot(engine, job_id)

    resp = _reset(engine, storage, job_id)

    assert resp.status_code == 409 and "cannot be checked" in resp.body.decode()
    assert _snapshot(engine, job_id) == before


class _FormRequest:
    def __init__(self, form: FormData) -> None:
        self._form = form
        self.url = type("U", (), {"path": "/jobs/x"})()

    async def form(self) -> FormData:
        return self._form


_UNREACHABLE = ScopeProblem(
    kind="unreachable",
    screen_ids=["0002"],
    message="Screen 'Settings' (0002) is reachable from no tab ('Home' home).",
    suggest_include=["faq"],
)


def _approve(engine: Engine, storage: _Storage, job_id: uuid.UUID, include: list[str]) -> Any:
    user = _user()
    form = FormData(
        [
            ("csrf_token", user.csrf_token),
            *[(f"include_{sid}", "on") for sid in include],
            ("tier_0", "2"),
            ("module_0", "push"),
        ]
    )
    with Session(engine) as db:
        return asyncio.run(
            routes_jobs.jobs_scope_approve(
                request=_FormRequest(form),  # type: ignore[arg-type]
                job_id=job_id,
                csrf_token=user.csrf_token,
                scope_mode="core",
                notes="drop FAQ for v1",
                user=user,
                db=db,
                storage=storage,  # type: ignore[arg-type]
            )
        )


@needs_db
def test_dogo_failed_job_reopen_then_reapprove(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.test_admin_scope import _page_context

    storage = _Storage()
    job_id = _make_job(engine, storage, S.FAILED, scope_status="approved")
    with Session(engine) as db:
        db.add(
            StageTimeline(
                job_id=job_id,
                stage=Stage.CODEGEN,
                started_at=datetime.now(UTC),
                finished_at=datetime.now(UTC),
                error="codegen failed: screens reachable from no tab: ['0002']",
            )
        )
        db.commit()
    calls = _wire_routes(monkeypatch, _settings(), _clean)

    resp = _reopen(engine, storage, job_id)

    assert resp.status_code == 303
    assert resp.headers["location"] == f"/jobs/{job_id}?reopened=1#t-scope"
    with Session(engine) as db:
        job = db.get(Job, job_id)
        assert job is not None
        meta = job.source_app_metadata
        assert job.state == S.NEEDS_INPUT
        assert meta["scope_status"] == "proposed"
        assert meta["scope_version_id"] == "v2"
        assert meta["run_epoch"] == 1
        for key in ("scope_approved_at", "scope_approved_by", "scope_routing_error"):
            assert key not in meta
        history = meta["scope_history"]
        assert history[-1]["scope_version_id"] == "v1"
        assert history[-1]["scope_approved_by"] == "anna"
        assert history[-1]["scope_routing_error"] == "old routing error"
        assert history[-1]["reopened_by"] == "op"
        assert meta["last_recovery"]["action"] == "scope_reopened"
        event = db.scalars(
            select(StageTimeline)
            .where(StageTimeline.job_id == job_id)
            .order_by(StageTimeline.created_at.desc())
        ).first()
        assert event is not None and event.stage == Stage.SCOPE
        assert event.error is not None and event.error.startswith(OPERATOR_EVENT_PREFIX)
        audit = db.scalars(select(AuditLog)).one()
        assert audit.action == "job.scope_reopened" and audit.actor == "op"
        assert audit.object_ref["scope_version_from"] == "v1"
        assert audit.object_ref["scope_version_to"] == "v2"
        assert audit.object_ref["epoch"] == 1
    reopened = feasibility.load_scope(storage, str(job_id))  # type: ignore[arg-type]
    original = _approved_scope()
    assert reopened.status == "proposed"
    assert reopened.approved_at is None and reopened.approved_by is None
    assert reopened.screens == original.screens
    assert reopened.scope_mode == original.scope_mode and reopened.notes == original.notes
    assert reopened.feasibility == original.feasibility
    assert feasibility.load_scope(storage, str(job_id), version_id="v1").status == "approved"  # type: ignore[arg-type]

    def _problems(_spec: dict[str, Any], scope: ScopeDecision | None) -> list[ScopeProblem]:
        included = {s.screen_id for s in scope.screens if s.include} if scope else set()
        return [_UNREACHABLE] if "0002" in included and "faq" not in included else []

    monkeypatch.setattr("iosforge.mvp.scope_check.load_full_spec", lambda *_a, **_k: {})
    monkeypatch.setattr("iosforge.mvp.scope_check.navigation_problems", _problems)
    monkeypatch.setattr("iosforge.mvp.feasibility.save_routed_spec", lambda *_a, **_k: None)
    monkeypatch.setattr(routes_jobs, "_detail_context", _page_context)

    blocked = _approve(engine, storage, job_id, ["home", "0002"])

    assert blocked.status_code == 422
    assert "0002" in blocked.body.decode()
    assert calls["build"] == []
    with Session(engine) as db:
        job = db.get(Job, job_id)
        assert job is not None and job.state == S.NEEDS_INPUT
        assert job.source_app_metadata["scope_status"] == "proposed"

    approved = _approve(engine, storage, job_id, ["home", "0002", "faq"])

    assert approved.status_code == 303
    assert calls["build"] == [(job_id, 1)]
    with Session(engine) as db:
        job = db.get(Job, job_id)
        assert job is not None and job.state == S.CODEGEN
        assert job.source_app_metadata["scope_status"] == "approved"
        assert job.source_app_metadata["run_epoch"] == 1
    final = feasibility.load_scope(storage, str(job_id))  # type: ignore[arg-type]
    assert final.status == "approved" and final.approved_by == "op"
    assert {s.screen_id for s in final.screens if s.include} == {"home", "0002", "faq"}


@needs_db
@pytest.mark.parametrize(
    ("state", "kwargs", "settings", "fragment"),
    [
        (S.DONE, {"scope_status": "approved"}, {}, "only a failed job"),
        (S.FAILED, {"scope_status": "approved"}, {"pipeline_scope_gate": False}, "gate is off"),
        (S.FAILED, {"scope_status": "approved", "built": True}, {}, "already built"),
        (S.FAILED, {}, {}, "before a scope was proposed"),
    ],
)
def test_reopen_refusals(
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    state: JobState,
    kwargs: dict[str, Any],
    settings: dict[str, Any],
    fragment: str,
) -> None:
    storage = _Storage()
    job_id = _make_job(engine, storage, state, **kwargs)
    _wire_routes(monkeypatch, _settings(**settings), _clean)
    before = _snapshot(engine, job_id)

    resp = _reopen(engine, storage, job_id)

    assert resp.status_code == 409
    body = resp.body.decode()
    assert body.startswith("Re-open is not possible now: ") and fragment in body
    assert _snapshot(engine, job_id) == before


@needs_db
def test_reopen_refuses_while_a_retry_is_scheduled(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = _Storage()
    job_id = _make_job(engine, storage, S.FAILED, scope_status="approved")
    retry = JobTasks(live=(TaskRef("r-1", "iosforge.run_job", "codegen@mac", "scheduled"),))
    _wire_routes(monkeypatch, _settings(), lambda _jid: retry)
    before = _snapshot(engine, job_id)

    resp = _reopen(engine, storage, job_id)

    assert resp.status_code == 409 and "scheduled" in resp.body.decode()
    assert _snapshot(engine, job_id) == before


@needs_db
def test_reopen_refuses_an_unreadable_scope(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = _Storage()
    job_id = _make_job(
        engine, storage, S.FAILED, meta={"scope_status": "approved", "scope_version_id": "v9"}
    )
    _wire_routes(monkeypatch, _settings(), _clean)

    resp = _reopen(engine, storage, job_id)

    assert resp.status_code == 409 and "cannot be read" in resp.body.decode()


@needs_db
def test_detail_context_offers_the_recovery_buttons(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(routes_jobs, "get_settings", _settings)
    storage = _Storage()
    stuck = _make_job(engine, storage, S.CODEGEN, scope_status="approved")
    _backdate(engine, stuck)
    fresh = _make_job(engine, storage, S.ANALYSIS)
    failed = _make_job(engine, storage, S.FAILED, scope_status="approved")
    with Session(engine) as db:
        job = db.get(Job, stuck)
        assert job is not None
        ctx = routes_jobs._recovery_context(db, job, None)
        assert ctx["can_reset"] is True and ctx["can_reopen"] is False
        assert int(str(ctx["last_activity_s"])) >= 3600
        job = db.get(Job, fresh)
        assert job is not None
        assert routes_jobs._recovery_context(db, job, None)["can_reset"] is False
        job = db.get(Job, failed)
        assert job is not None
        ctx = routes_jobs._recovery_context(db, job, None)
        assert ctx["can_reset"] is False and ctx["can_reopen"] is True
