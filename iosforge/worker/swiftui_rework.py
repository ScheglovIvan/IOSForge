"""Operator rework rounds of a SwiftUI app (Phase 6): extend the scope and/or edit, then ship.

A round starts only from the admin, only for a job that is not running (DONE / FAILED /
NEEDS_INPUT, no scope proposal awaiting approval, no delivery in flight) and only on a
Mac (the toolchain and a simulator are checked before the job is touched). The job is
claimed atomically, so a double submit runs one round. The round starts from the app's
recorded built scope; ``add_screens`` grows it with screens of the full app_spec,
``instructions`` run one model round over model-owned code. A single Vision-Judge pass
is recorded, the new version is stored (``result_version`` + 1, its own sources key,
the new built scope) and native delivery is queued. A failed round restores the job's
previous state, so the last good version stays deliverable.
"""

from __future__ import annotations

import json
import tempfile
import uuid
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from iosforge.common.config import get_settings
from iosforge.common.logging import get_logger
from iosforge.common.queue import PipelineTask, celery_app
from iosforge.common.types import JobState, Stage
from iosforge.db.base import utcnow
from iosforge.db.models import GenerationResult, Job, StageTimeline
from iosforge.db.session import get_sessionmaker
from iosforge.mvp import compliance, simulator, swiftui_gen
from iosforge.mvp.paths import RunPaths
from iosforge.storage.client import ArtifactStorage, S3ArtifactStorage, build_key
from iosforge.worker.swiftui_tasks import (
    XCODE_QUEUE,
    active_build,
    claim_job,
    effective_scope_ids,
    hydrate_inputs,
    hydrate_sources,
    job_locale,
    queue_delivery,
    save_built_scope,
    sources_key,
    store_sources,
)

log = get_logger("worker.swiftui_rework")

REWORKABLE = (JobState.DONE, JobState.FAILED, JobState.NEEDS_INPUT)


def rework_refusal(db: Session, job: Job, storage: ArtifactStorage) -> str | None:
    """Why a round cannot start now (None when it can)."""
    if job.state not in REWORKABLE:
        return f"job is {job.state}"
    if (job.source_app_metadata or {}).get("scope_status") == "proposed":
        return "the scope proposal awaits approval"
    if active_build(db, job.id) is not None:
        return "a native delivery is in flight"
    if not storage.exists(sources_key(str(job.id))):
        return "no generated SwiftUI sources"
    return None


def record_version(
    db: Session, job: Job, key: str, report: dict[str, Any], version: int
) -> GenerationResult:
    """Point the job's single ``GenerationResult`` at ``version``, keeping the history.

    Earlier versions stay restorable: their sources keys and scores are listed under
    ``selftest_report["versions"]``.
    """
    gen = db.scalar(select(GenerationResult).where(GenerationResult.job_id == job.id))
    if gen is None:
        gen = GenerationResult(job_id=job.id, sources_key=key)
        db.add(gen)
        history: list[dict[str, Any]] = []
    else:
        previous = gen.selftest_report or {}
        history = list(previous.get("versions") or [])
        history.append(
            {
                "version": version - 1,
                "sources_key": gen.sources_key,
                "compliance_score": gen.compliance_score,
            }
        )
    gen.sources_key = key
    gen.compliance_score = report.get("compliance_score")
    gen.selftest_report = {
        **report, "verify": "ios", "round": "rework", "version": version, "versions": history,
    }  # fmt: skip
    return gen


def _additions(
    job: Job, storage: ArtifactStorage, add_screens: list[str], settings: Any
) -> list[str]:
    spec = json.loads(
        storage.get(build_key(job_id=str(job.id), kind="app_spec", name="app_spec.json"))
    )
    known = {str(s["id"]) for s in spec.get("screens", []) if isinstance(s, dict)}
    current = set(effective_scope_ids(job, storage, spec, settings))
    return list(dict.fromkeys(s for s in add_screens if s in known and s not in current))


@celery_app.task(base=PipelineTask, name="iosforge.rework_swiftui", bind=True, queue=XCODE_QUEUE)
def rework_swiftui(
    self: Any, job_id: str, instructions: str = "", add_screens: list[str] | None = None
) -> str:
    """Run one operator round over the job's stored SwiftUI app, then queue delivery."""
    settings = get_settings()
    storage = S3ArtifactStorage()
    maker = get_sessionmaker()
    with maker() as db:
        job = db.get(Job, uuid.UUID(job_id))
        if job is None:
            return f"job {job_id} not found"
        refusal = rework_refusal(db, job, storage)
        if refusal:
            return f"job {job_id} cannot be reworked: {refusal}"
        added = _additions(job, storage, list(add_screens or []), settings)
        if not instructions.strip() and not added:
            return f"job {job_id}: nothing to rework"
        try:
            simulator.require_toolchain()
            udid = simulator.resolve_udid(settings.ios_simulator_udid)
        except simulator.SimulatorUnavailable as exc:
            return f"job {job_id} cannot be reworked here: {exc}"
        previous = job.state
        if not claim_job(db, job.id, REWORKABLE, JobState.CODEGEN):
            return f"job {job_id} cannot be reworked: another round claimed it"
        if active_build(db, job.id) is not None:
            job.state = previous
            db.commit()
            return f"job {job_id} cannot be reworked: a native delivery is in flight"
        stage = StageTimeline(job_id=job.id, stage=Stage.CODEGEN, started_at=utcnow())
        db.add(stage)
        db.commit()
        stage_id = stage.id
        try:
            with tempfile.TemporaryDirectory(prefix="iosforge-rework-") as tmp:
                paths = RunPaths.create(Path(tmp) / "run")
                hydrate_inputs(db, job, storage, paths)
                full_spec = json.loads(paths.app_spec_json.read_text(encoding="utf-8"))
                scope = effective_scope_ids(job, storage, full_spec, settings)
                swiftui_gen.scope_to(paths, scope)
                hydrate_sources(storage, job_id, paths.xcode_app)
                if added:
                    swiftui_gen.extend(paths, added, full_spec)
                    scope = [*scope, *added]
                if instructions.strip():
                    swiftui_gen.rework(
                        paths, instructions, timeout=settings.codegen_rework_timeout_s
                    )
                report = compliance.verify_ios(
                    paths,
                    simulator.SimEnvironment(udid=udid, locale=job_locale(job, paths)),
                    weights=compliance.ComplianceWeights.from_settings(settings),
                    threshold=settings.frontend_verify_threshold,
                    soft_floor=settings.compliance_soft_floor,
                )
                version = (job.result_version or 1) + 1
                key = store_sources(storage, job_id, paths.xcode_app, version=version)
                save_built_scope(storage, job_id, scope)
            job.result_version = version
            record_version(db, job, key, report, version)
            stage.finished_at = utcnow()
            job.state = JobState.DONE
            db.commit()
        except Exception as exc:
            log.error("swiftui.rework.failed", job_id=job_id, error=str(exc))
            db.rollback()
            failed_job = db.get(Job, uuid.UUID(job_id))
            row = db.get(StageTimeline, stage_id)
            if row is not None:
                row.error = str(exc)[:4000]
                row.finished_at = utcnow()
            if failed_job is not None:
                failed_job.state = previous
            db.commit()
            return f"job {job_id} rework failed: {exc}"
        try:
            if queue_delivery(db, job) is None:
                log.warning("swiftui.rework.delivery_skipped_active", job_id=job_id)
        except Exception as exc:
            log.error("swiftui.rework.delivery_enqueue_failed", job_id=job_id, error=str(exc))
    return f"job {job_id} reworked (v{job.result_version})"
