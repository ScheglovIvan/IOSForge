"""Bring a stuck or failed job back from the admin: reset stuck job, re-open scope.

A job whose Celery task died (kill, OOM, restart) stays in an intermediate state
forever, and a FAILED job is a dead end. Two operator actions recover it without
re-running the analysis:

* :func:`reset_stuck_job` — for a job idle in an intermediate state (or with a native
  delivery that never finished) and with no task of it running or waiting. Where it
  goes is :func:`plan_reset` (a pure transition table).
* :func:`reopen_scope` — for a FAILED job whose app was never built: the approved
  scope goes back to *proposed* with the operator's selection kept, the job waits in
  ``NEEDS_INPUT`` for a new approval.

Both bump the job's **run epoch** (``source_app_metadata["run_epoch"]``). The state
tasks receive the epoch they were queued with and do nothing when it is no longer
current (:func:`is_stale`), so a message of a killed task that Redis redelivers hours
later cannot overwrite the recovered job. Both write a muted timeline row (error
starting with :data:`OPERATOR_EVENT_PREFIX`), an ``audit_logs`` entry and
``meta["last_recovery"]``, commit, and return the task to queue in
:class:`RecoveryOutcome` so the caller enqueues it only after the commit.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any, Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from iosforge.common.logging import get_logger
from iosforge.common.types import JobState, Stage
from iosforge.db.base import utcnow
from iosforge.db.models import AuditLog, GenerationResult, Job, StageTimeline, XcodeBuild

if TYPE_CHECKING:
    from iosforge.common.config import Settings
    from iosforge.mvp.scope_models import ScopeDecision
    from iosforge.orchestrator.liveness import JobTasks, TaskProbe
    from iosforge.storage.client import ArtifactStorage

log = get_logger("orchestrator.recovery")

OPERATOR_EVENT_PREFIX = "operator: "
EPOCH_KEY = "run_epoch"
ACTIVE_BUILD_STATUSES = ("queued", "archiving")

RESTART_STATES: frozenset[JobState] = frozenset(
    {
        JobState.QUEUED,
        JobState.RETRYABLE,
        JobState.DISCOVERY,
        JobState.ACQUISITION,
        JobState.WALKTHROUGH,
        JobState.ANALYSIS,
    }
)
BUILD_STATES: frozenset[JobState] = frozenset(
    {
        JobState.CODEGEN,
        JobState.VERIFY,
        JobState.GITHUB_UPLOAD,
        JobState.CODEMAGIC_INTEGRATION,
        JobState.DELIVERY,
    }
)
RESETTABLE_STATES: frozenset[JobState] = RESTART_STATES | BUILD_STATES | {JobState.SCOPE}
REOPENABLE_SCOPE = ("approved", "proposed")

RecoveryCode = Literal[
    "not_found",
    "not_stuck",
    "too_recent",
    "live",
    "queued",
    "unverifiable",
    "moved",
    "not_failed",
    "gate_off",
    "built",
    "no_scope",
    "scope_unreadable",
]
RunAction = Literal["run_job", "scope_gate"]


class RecoveryError(Exception):
    """The recovery action is refused; ``message`` is shown to the operator (409)."""

    def __init__(self, code: RecoveryCode, message: str) -> None:
        super().__init__(message)
        self.code: RecoveryCode = code
        self.message = message


def epoch_of(meta: object) -> int:
    """The run epoch stored in a job's metadata (0 when absent or malformed)."""
    value = meta.get(EPOCH_KEY, 0) if isinstance(meta, dict) else 0
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def current_epoch(job: Job) -> int:
    """The job's current run epoch; tasks are queued with it."""
    return epoch_of(job.source_app_metadata)


def is_stale(job: Job, epoch: int | None) -> bool:
    """True when a task queued with ``epoch`` was superseded by a recovery.

    ``None`` (a message queued before epochs existed) is never stale.
    """
    return epoch is not None and epoch != current_epoch(job)


def superseded(db: Session, job_id: uuid.UUID, epoch: int | None) -> bool:
    """Like :func:`is_stale`, but against the committed row (re-read, not the session copy).

    Tasks call it right before their final commit: a recovery that happened while
    they ran must win.
    """
    if epoch is None:
        return False
    meta = db.scalar(select(Job.source_app_metadata).where(Job.id == job_id))
    return epoch != epoch_of(meta)


@dataclass(frozen=True, slots=True)
class ResetPlan:
    """Where a reset takes the job, and what else it does."""

    to_state: JobState
    action: RunAction | None = None
    reopen_scope: bool = False
    fail_build: bool = False


def plan_reset(
    state: JobState,
    *,
    built: bool,
    held: bool,
    scope_gate: bool,
    scope_status: str | None,
    active_build: bool,
) -> ResetPlan:
    """The reset transition for a job in ``state`` (pure; raises when nothing to reset).

    ``built`` — a ``GenerationResult`` exists; ``held`` — its report holds the app for
    the operator; ``active_build`` — a native delivery is queued or archiving.
    """
    if state in RESTART_STATES:
        return ResetPlan(JobState.QUEUED, "run_job", fail_build=active_build)
    if state == JobState.SCOPE:
        return ResetPlan(JobState.SCOPE, "scope_gate", fail_build=active_build)
    if state in BUILD_STATES:
        if built:
            target = JobState.NEEDS_INPUT if held else JobState.DONE
            return ResetPlan(target, fail_build=active_build)
        if scope_gate and scope_status == "approved":
            return ResetPlan(JobState.NEEDS_INPUT, reopen_scope=True, fail_build=active_build)
        return ResetPlan(JobState.FAILED, fail_build=active_build)
    if active_build:
        return ResetPlan(state, fail_build=True)
    raise RecoveryError("not_stuck", f"the job is {state.value} and no native delivery is running")


@dataclass(frozen=True, slots=True)
class RecoveryOutcome:
    """What a recovery did; ``action`` is the task the caller queues after the commit."""

    job_id: uuid.UUID
    from_state: JobState
    to_state: JobState
    epoch: int
    action: RunAction | None = None
    reopened: bool = False
    build_failed: bool = False
    orphaned_task_ids: tuple[str, ...] = ()
    scope_version_from: str | None = None
    scope_version_to: str | None = None
    notes: tuple[str, ...] = field(default_factory=tuple)


def active_build(db: Session, job_id: uuid.UUID) -> XcodeBuild | None:
    """The job's native delivery that is still queued or archiving (if any)."""
    return db.scalar(
        select(XcodeBuild)
        .where(XcodeBuild.job_id == job_id, XcodeBuild.status.in_(ACTIVE_BUILD_STATUSES))
        .order_by(XcodeBuild.created_at.desc())
    )


def _open_stages(db: Session, job_id: uuid.UUID) -> list[StageTimeline]:
    return list(
        db.scalars(
            select(StageTimeline).where(
                StageTimeline.job_id == job_id, StageTimeline.finished_at.is_(None)
            )
        ).all()
    )


def last_activity(db: Session, job: Job, build: XcodeBuild | None = None) -> datetime:
    """Latest sign of life: the job row, an open stage's start, an in-flight delivery."""
    moments = [job.updated_at]
    moments.extend(row.started_at for row in _open_stages(db, job.id) if row.started_at)
    if build is not None:
        moments.append(build.updated_at)
    return max(moments)


def idle_seconds(db: Session, job: Job, now: datetime, build: XcodeBuild | None = None) -> float:
    """Seconds since :func:`last_activity`."""
    return (now - last_activity(db, job, build)).total_seconds()


def _refuse_busy(tasks: JobTasks, verb: str) -> None:
    if tasks.live:
        ref = tasks.live[0]
        raise RecoveryError(
            "live", f"a {ref.name} task of this job is {ref.kind} on worker {ref.where}"
        )
    if tasks.queued:
        ref = tasks.queued[0]
        raise RecoveryError(
            "queued",
            f"a {ref.name} task of this job is waiting in queue {ref.where}; {verb} after it ran",
        )


def _lock(db: Session, job_id: uuid.UUID, state: JobState, updated_at: datetime) -> Job:
    job = db.get(Job, job_id, with_for_update=True, populate_existing=True)
    if job is None or job.state != state or job.updated_at != updated_at:
        db.rollback()
        raise RecoveryError("moved", "the job moved on while it was checked; reload and retry")
    return job


def _event_stage(state: JobState) -> Stage:
    if state in (JobState.QUEUED, JobState.RETRYABLE):
        return Stage.DISCOVERY
    if state in (JobState.DONE, JobState.NEEDS_INPUT, JobState.FAILED, JobState.CANCELLED):
        return Stage.DELIVERY
    return Stage(state.value)


def _record(
    db: Session,
    job: Job,
    *,
    stage: Stage,
    actor: str,
    action: str,
    text: str,
    now: datetime,
    details: dict[str, Any],
) -> None:
    db.add(
        StageTimeline(
            job_id=job.id,
            stage=stage,
            started_at=now,
            finished_at=now,
            duration_ms=0,
            error=f"{OPERATOR_EVENT_PREFIX}{text}"[:4000],
        )
    )
    db.add(
        AuditLog(
            actor=actor, action=action, object_ref={"type": "job", "id": str(job.id), **details}
        )
    )


def _bump_epoch(meta: dict[str, Any], record: dict[str, Any]) -> int:
    epoch = epoch_of(meta) + 1
    meta[EPOCH_KEY] = epoch
    meta["last_recovery"] = {**record, "epoch": epoch}
    return epoch


def _load_scope(storage: ArtifactStorage, job: Job) -> ScopeDecision:
    from iosforge.mvp import feasibility

    version = (job.source_app_metadata or {}).get("scope_version_id")
    try:
        return feasibility.load_scope(
            storage, str(job.id), version_id=str(version) if version else None
        )
    except Exception as exc:
        raise RecoveryError("scope_unreadable", f"the stored scope cannot be read ({exc})") from exc


def _reopen(
    storage: ArtifactStorage,
    job: Job,
    meta: dict[str, Any],
    scope: ScopeDecision,
    *,
    actor: str,
    now: datetime,
) -> tuple[str | None, str | None]:
    """Save ``scope`` as a new *proposed* version and point ``meta`` at it (in place)."""
    from iosforge.mvp import feasibility

    version_from = meta.get("scope_version_id")
    previous = {
        "scope_status": meta.get("scope_status"),
        "scope_version_id": version_from,
        "scope_approved_at": meta.get("scope_approved_at"),
        "scope_approved_by": meta.get("scope_approved_by"),
        "scope_routing_error": meta.get("scope_routing_error"),
        "reopened_at": now.isoformat(),
        "reopened_by": actor,
    }
    scope.status = "proposed"
    scope.approved_at = None
    scope.approved_by = None
    ref = feasibility.save_scope(storage, str(job.id), scope)
    history = meta.get("scope_history")
    meta["scope_history"] = [*(history if isinstance(history, list) else []), previous]
    meta["scope_status"] = "proposed"
    meta["scope_version_id"] = ref.version_id
    for key in ("scope_approved_at", "scope_approved_by", "scope_routing_error"):
        meta.pop(key, None)
    return (str(version_from) if version_from else None), ref.version_id


def _close_open_stages(db: Session, job_id: uuid.UUID, actor: str, now: datetime) -> int:
    rows = _open_stages(db, job_id)
    for row in rows:
        row.finished_at = now
        if row.started_at:
            row.duration_ms = int((now - row.started_at).total_seconds() * 1000)
        row.error = f"interrupted — reset by {actor}"
    return len(rows)


def _held(gen: GenerationResult, settings: Settings) -> bool:
    from iosforge.worker.swiftui_build import hold_reason

    report = gen.selftest_report or {}
    return hold_reason(report, structural_gate=settings.web_verify_hard_gate) is not None


def _plan_for(db: Session, job: Job, build: XcodeBuild | None, settings: Settings) -> ResetPlan:
    gen = db.scalar(select(GenerationResult).where(GenerationResult.job_id == job.id))
    return plan_reset(
        job.state,
        built=gen is not None,
        held=gen is not None and _held(gen, settings),
        scope_gate=settings.pipeline_scope_gate,
        scope_status=(job.source_app_metadata or {}).get("scope_status"),
        active_build=build is not None,
    )


def reset_stuck_job(
    db: Session,
    storage: ArtifactStorage,
    job_id: uuid.UUID,
    *,
    actor: str,
    probe: TaskProbe,
    settings: Settings,
    now: datetime | None = None,
) -> RecoveryOutcome:
    """Reset a job whose task is gone (see :func:`plan_reset`); commits.

    Refused (:class:`RecoveryError`) unless the job is in a resettable state or has a
    native delivery in flight, has been idle for ``job_reset_min_idle_s`` and the
    probe finds no task of it running or queued.
    """
    now = now or utcnow()
    job = db.get(Job, job_id)
    if job is None:
        raise RecoveryError("not_found", "job not found")
    build = active_build(db, job.id)
    if job.state not in RESETTABLE_STATES and build is None:
        raise RecoveryError("not_stuck", f"the job is {job.state.value}, not stuck in a stage")
    idle = idle_seconds(db, job, now, build)
    if idle < settings.job_reset_min_idle_s:
        raise RecoveryError(
            "too_recent",
            f"the job was active {int(idle)} s ago; a job counts as stuck after "
            f"{settings.job_reset_min_idle_s} s without activity",
        )
    seen_state, seen_updated = job.state, job.updated_at
    tasks = probe(str(job.id))
    _refuse_busy(tasks, "reset")
    job = _lock(db, job_id, seen_state, seen_updated)
    build = active_build(db, job.id)
    plan = _plan_for(db, job, build, settings)
    notes: list[str] = []
    scope: ScopeDecision | None = None
    if plan.reopen_scope:
        try:
            scope = _load_scope(storage, job)
        except RecoveryError as exc:
            notes.append(exc.message)
            plan = ResetPlan(JobState.FAILED, fail_build=plan.fail_build)

    from_state = job.state
    meta = dict(job.source_app_metadata or {})
    version_from: str | None = None
    version_to: str | None = None
    if scope is not None:
        version_from, version_to = _reopen(storage, job, meta, scope, actor=actor, now=now)
    closed = _close_open_stages(db, job.id, actor, now)
    if plan.fail_build and build is not None:
        build.status = "failed"
        build.message = f"reset by {actor}: the delivery stopped without a result"
        build.finished_at = now
        if build.started_at:
            build.duration_ms = int((now - build.started_at).total_seconds() * 1000)
    orphaned = tuple(ref.task_id for ref in tasks.orphaned)
    epoch = _bump_epoch(
        meta,
        {
            "action": "reset",
            "by": actor,
            "at": now.isoformat(),
            "from": from_state.value,
            "to": plan.to_state.value,
        },
    )
    job.source_app_metadata = meta
    job.state = plan.to_state
    text = f"reset by {actor}: {from_state.value} → {plan.to_state.value}"
    if scope is not None:
        text += "; scope re-opened for approval"
    if plan.fail_build and build is not None:
        text += "; native delivery marked failed"
    if notes:
        text += f"; {'; '.join(notes)}"
    _record(
        db,
        job,
        stage=_event_stage(from_state),
        actor=actor,
        action="job.reset",
        text=text,
        now=now,
        details={
            "from": from_state.value,
            "to": plan.to_state.value,
            "epoch": epoch,
            "action": plan.action,
            "orphaned_task_ids": list(orphaned),
            "workers_replied": list(tasks.workers_replied),
            "closed_stages": closed,
            "build_failed": str(build.id) if plan.fail_build and build is not None else None,
            "scope_version_from": version_from,
            "scope_version_to": version_to,
            "idle_s": int(idle),
            "notes": notes,
        },
    )
    db.commit()
    log.info(
        "recovery.reset",
        job_id=str(job_id),
        actor=actor,
        from_state=from_state.value,
        to_state=plan.to_state.value,
        epoch=epoch,
        orphaned=list(orphaned),
    )
    return RecoveryOutcome(
        job_id=job_id,
        from_state=from_state,
        to_state=plan.to_state,
        epoch=epoch,
        action=plan.action,
        reopened=scope is not None,
        build_failed=plan.fail_build and build is not None,
        orphaned_task_ids=orphaned,
        scope_version_from=version_from,
        scope_version_to=version_to,
        notes=tuple(notes),
    )


def reopen_refusal(db: Session, job: Job, settings: Settings) -> RecoveryError | None:
    """Why the job's scope cannot be re-opened (None when it can; no I/O besides the DB)."""
    if job.state != JobState.FAILED:
        return RecoveryError(
            "not_failed", f"only a failed job can be re-opened (it is {job.state.value})"
        )
    if not settings.pipeline_scope_gate:
        return RecoveryError("gate_off", "the scope gate is off")
    if db.scalar(select(GenerationResult.id).where(GenerationResult.job_id == job.id)):
        return RecoveryError("built", "the app is already built; use Rework or Extend scope")
    if (job.source_app_metadata or {}).get("scope_status") not in REOPENABLE_SCOPE:
        return RecoveryError("no_scope", "the job failed before a scope was proposed")
    return None


def reopen_scope(
    db: Session,
    storage: ArtifactStorage,
    job_id: uuid.UUID,
    *,
    actor: str,
    probe: TaskProbe,
    settings: Settings,
    now: datetime | None = None,
) -> RecoveryOutcome:
    """Put a FAILED job's scope back up for approval (selection kept); commits.

    The scope version the job pinned (else the latest) is saved again as a new
    ``proposed`` version; the job goes to ``NEEDS_INPUT``. Nothing is queued: the
    operator approves the scope again from the Scope tab.
    """
    now = now or utcnow()
    job = db.get(Job, job_id)
    if job is None:
        raise RecoveryError("not_found", "job not found")
    refusal = reopen_refusal(db, job, settings)
    if refusal is not None:
        raise refusal
    scope = _load_scope(storage, job)
    seen_state, seen_updated = job.state, job.updated_at
    tasks = probe(str(job.id))
    _refuse_busy(tasks, "re-open")
    job = _lock(db, job_id, seen_state, seen_updated)
    refusal = reopen_refusal(db, job, settings)
    if refusal is not None:
        db.rollback()
        raise refusal

    meta = dict(job.source_app_metadata or {})
    version_from, version_to = _reopen(storage, job, meta, scope, actor=actor, now=now)
    orphaned = tuple(ref.task_id for ref in tasks.orphaned)
    epoch = _bump_epoch(
        meta,
        {
            "action": "scope_reopened",
            "by": actor,
            "at": now.isoformat(),
            "from": JobState.FAILED.value,
            "to": JobState.NEEDS_INPUT.value,
        },
    )
    job.source_app_metadata = meta
    job.state = JobState.NEEDS_INPUT
    _record(
        db,
        job,
        stage=Stage.SCOPE,
        actor=actor,
        action="job.scope_reopened",
        text=f"scope re-opened by {actor}: failed → needs_input, awaiting a new approval",
        now=now,
        details={
            "from": JobState.FAILED.value,
            "to": JobState.NEEDS_INPUT.value,
            "epoch": epoch,
            "orphaned_task_ids": list(orphaned),
            "workers_replied": list(tasks.workers_replied),
            "scope_version_from": version_from,
            "scope_version_to": version_to,
        },
    )
    db.commit()
    log.info(
        "recovery.scope_reopened",
        job_id=str(job_id),
        actor=actor,
        epoch=epoch,
        scope_version_from=version_from,
        scope_version_to=version_to,
    )
    return RecoveryOutcome(
        job_id=job_id,
        from_state=JobState.FAILED,
        to_state=JobState.NEEDS_INPUT,
        epoch=epoch,
        reopened=True,
        orphaned_task_ids=orphaned,
        scope_version_from=version_from,
        scope_version_to=version_to,
    )


__all__ = [
    "OPERATOR_EVENT_PREFIX",
    "RESETTABLE_STATES",
    "RecoveryError",
    "RecoveryOutcome",
    "ResetPlan",
    "active_build",
    "current_epoch",
    "epoch_of",
    "idle_seconds",
    "is_stale",
    "last_activity",
    "plan_reset",
    "reopen_refusal",
    "reopen_scope",
    "reset_stuck_job",
    "superseded",
]
