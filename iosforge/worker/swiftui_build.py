"""The SwiftUI frontend build of a job (Phase 7): codegen → Vision Judge → native delivery.

Runs on the Mac ``xcode`` queue after the analysis (and, with the scope gate, after the
operator approved the scope). Restores the job's inputs, prunes them to the approved
screens (``app_spec.json`` and the judge's ``screens.json`` alike), generates the app
in sandboxed screen tasks, refines it against the Vision Judge on the iOS Simulator
with the unchanged ``_refine`` loop, stores the sources as a ``GenerationResult`` and
queues the native archive/IPA. With ``web_verify_hard_gate`` a structurally incomplete
app parks in ``NEEDS_INPUT`` for the operator (ship or rework) instead of shipping.
"""

from __future__ import annotations

import json
import tempfile
import uuid
from pathlib import Path
from typing import Any

from sqlalchemy import select

from iosforge.common.config import get_settings
from iosforge.common.logging import get_logger
from iosforge.common.queue import PipelineTask, celery_app
from iosforge.common.types import JobState, Stage
from iosforge.db.base import utcnow
from iosforge.db.models import GenerationResult, Job, StageTimeline
from iosforge.db.session import get_sessionmaker
from iosforge.mvp import build_profile, compliance, simulator, swiftui_gen
from iosforge.mvp.paths import RunPaths
from iosforge.storage.client import S3ArtifactStorage, build_key
from iosforge.worker.swiftui_tasks import (
    XCODE_QUEUE,
    hydrate_inputs,
    job_locale,
    queue_delivery,
    save_built_scope,
    scope_ids,
    store_sources,
)

log = get_logger("worker.swiftui_build")


def enqueue_build(job_id: str) -> None:
    """Queue the job's SwiftUI build on the Mac queue (pipeline chain and admin)."""
    build_swiftui.apply_async(args=[job_id], queue=XCODE_QUEUE)


def _store_screens(storage: Any, job_id: str, paths: RunPaths) -> None:
    for png in sorted(paths.generated_screens_dir.glob("*.png")):
        storage.put(
            build_key(job_id=job_id, kind="generated_screenshots", name=png.name),
            png.read_bytes(),
            content_type="image/png",
        )


@celery_app.task(base=PipelineTask, name="iosforge.build_swiftui", bind=True, queue=XCODE_QUEUE)
def build_swiftui(self: Any, job_id: str) -> str:
    """Generate, refine and store the job's SwiftUI app, then queue its delivery."""
    settings = get_settings()
    storage = S3ArtifactStorage()
    maker = get_sessionmaker()
    with maker() as db:
        job = db.get(Job, uuid.UUID(job_id))
        if job is None:
            return f"job {job_id} not found"
        if db.scalar(select(GenerationResult).where(GenerationResult.job_id == job.id)):
            log.info("build_swiftui.skip_existing", job_id=job_id)
            return f"job {job_id} already built"
        job.state = JobState.CODEGEN
        job.codegen_target = "swiftui"
        stage = StageTimeline(job_id=job.id, stage=Stage.CODEGEN, started_at=utcnow())
        db.add(stage)
        db.commit()
        try:
            simulator.require_toolchain()
            with tempfile.TemporaryDirectory(prefix="iosforge-swiftui-") as tmp:
                paths = RunPaths.create(Path(tmp) / "run")
                hydrate_inputs(db, job, storage, paths)
                spec = json.loads(paths.app_spec_json.read_text(encoding="utf-8"))
                scope = scope_ids(job, storage, spec, settings)
                swiftui_gen.scope_to(paths, scope)
                spec = json.loads(paths.app_spec_json.read_text(encoding="utf-8"))
                ident = build_profile.resolve_identity(job.source_app_metadata, spec, settings)
                result = swiftui_gen.generate(
                    paths,
                    app_name=ident.app_name,
                    bundle_id=ident.bundle_id,
                    max_parallel=settings.codegen_max_parallel,
                    settings=settings,
                )
                if result.errors:
                    raise RuntimeError(f"codegen failed: {result.errors[:10]}")
                report = compliance.refine_ios_until_complete(
                    paths,
                    simulator.SimEnvironment(
                        udid=simulator.resolve_udid(settings.ios_simulator_udid),
                        locale=job_locale(job, paths),
                    ),
                    threshold=settings.frontend_verify_threshold,
                    soft_floor=settings.compliance_soft_floor,
                    max_iterations=settings.compliance_max_iterations,
                    weights=compliance.ComplianceWeights.from_settings(settings),
                    blank_max_bytes=settings.web_blank_max_bytes,
                    structural_gate=settings.web_verify_hard_gate,
                )
                _store_screens(storage, job_id, paths)
                version = job.result_version or 1
                key = store_sources(storage, job_id, paths.xcode_app, version=version)
                save_built_scope(storage, job_id, scope)
            gated = settings.web_verify_hard_gate and report.get("stop_reason") != "all_closed"
            db.add(
                GenerationResult(
                    job_id=job.id,
                    sources_key=key,
                    compliance_score=report.get("compliance_score"),
                    selftest_report={
                        **report,
                        "mode": "frontend_only",
                        "verify": "ios",
                        "version": version,
                    },  # fmt: skip
                )
            )
            stage.finished_at = utcnow()
            job.state = JobState.NEEDS_INPUT if gated else JobState.DONE
            db.commit()
        except Exception as exc:
            log.error("build_swiftui.failed", job_id=job_id, error=str(exc))
            stage.error = str(exc)[:4000]
            stage.finished_at = utcnow()
            job.state = JobState.FAILED
            db.commit()
            return f"job {job_id} build failed: {exc}"
        if gated:
            log.info("build_swiftui.gated", job_id=job_id, score=report.get("compliance_score"))
            return f"job {job_id} built; structural gaps hold delivery"
        try:
            queue_delivery(db, job)
        except Exception as exc:
            log.error("build_swiftui.delivery_enqueue_failed", job_id=job_id, error=str(exc))
        return f"job {job_id} built"
