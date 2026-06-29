"""Celery worker: run the MVP vertical for an uploaded Job (SPEC §7 wiring).

Pulls the APK from MinIO, boots the emulator, crawls screens, generates a Flutter
app, and writes stage timeline + artifacts back to the DB/MinIO. Reuses the
phase-1 vertical in ``iosforge.mvp`` and the EPIC-2 infra (DB/Celery/MinIO).
Requires a booted/bootable AVD on the worker host.
"""

from __future__ import annotations

import json
import shutil
import tempfile
import uuid
from pathlib import Path

from sqlalchemy import select

from iosforge.common.config import get_settings
from iosforge.common.logging import get_logger
from iosforge.common.queue import PipelineTask, celery_app
from iosforge.common.types import JobState, Stage
from iosforge.db.base import utcnow
from iosforge.db.models import ApkArtifact, GenerationResult, Job, StageTimeline, WalkthroughResult
from iosforge.db.session import get_sessionmaker
from iosforge.storage.client import S3ArtifactStorage, build_key

log = get_logger("worker.run_job")


def _start_stage(db, job: Job, stage: Stage, state: JobState) -> StageTimeline:
    job.state = state
    row = StageTimeline(job_id=job.id, stage=stage, started_at=utcnow())
    db.add(row)
    db.commit()
    return row


def _finish_stage(db, row: StageTimeline) -> None:
    row.finished_at = utcnow()
    if row.started_at:
        row.duration_ms = int((row.finished_at - row.started_at).total_seconds() * 1000)
    db.commit()


@celery_app.task(base=PipelineTask, name="iosforge.run_job", bind=True)
def run_job(self, job_id: str) -> str:
    from iosforge.mvp import analyze, codegen, compliance, crawl, emulator
    from iosforge.mvp.paths import RunPaths

    settings = get_settings()
    storage = S3ArtifactStorage()
    maker = get_sessionmaker()
    tmp = Path(tempfile.mkdtemp(prefix="iosforge-job-"))

    with maker() as db:
        job = db.get(Job, uuid.UUID(job_id))
        if job is None:
            return f"job {job_id} not found"
        apk_art = db.scalar(select(ApkArtifact).where(ApkArtifact.job_id == job.id))
        if apk_art is None:
            job.state = JobState.FAILED
            db.commit()
            return f"job {job_id} has no APK artifact"

        stage_row: StageTimeline | None = None
        try:
            apk_path = tmp / "app.apk"
            apk_path.write_bytes(storage.get(apk_art.storage_key))

            # --- Walkthrough ---
            stage_row = _start_stage(db, job, Stage.WALKTHROUGH, JobState.WALKTHROUGH)
            emulator.start_emulator(settings.admin_avd)
            emulator.wait_for_boot()
            package = emulator.install_apk(apk_path)
            emulator.launch(package)
            paths = RunPaths.create(tmp / "run")
            crawl.walk(paths, package, settings.walkthrough_max_screens)

            screenshot_keys = []
            for png in sorted(paths.screens_dir.glob("*.png")):
                key = build_key(job_id=job_id, kind="screenshots", name=png.name)
                storage.put(key, png.read_bytes(), content_type="image/png")
                screenshot_keys.append(key)
            screen_map = json.loads(paths.screens_json.read_text())
            db.add(
                WalkthroughResult(
                    job_id=job.id,
                    screenshot_keys=screenshot_keys,
                    screen_map=screen_map,
                    provider="adb-mvp",
                )
            )
            _finish_stage(db, stage_row)
            stage_row = None

            if settings.pipeline_stop_after_walkthrough:
                job.state = JobState.DONE
                db.commit()
                log.info("run_job.walkthrough_only_done", job_id=job_id)
                return f"job {job_id} done (walkthrough only)"

            # --- Codegen (Stage B->C->D->E inside one CODEGEN span) ---
            stage_row = _start_stage(db, job, Stage.CODEGEN, JobState.CODEGEN)
            analyze.analyze(paths)
            analyze.decompose(paths)
            codegen.generate(paths, settings)
            report = compliance.refine_until_compliant(
                paths,
                threshold=settings.compliance_threshold,
                soft_floor=settings.compliance_soft_floor,
                max_iterations=settings.compliance_max_iterations,
                weights=compliance.ComplianceWeights.from_settings(settings),
                avd=settings.admin_avd,
            )

            zip_base = tmp / "flutter_app"
            shutil.make_archive(str(zip_base), "zip", str(paths.flutter_app))
            sources_key = build_key(job_id=job_id, kind="sources", name="flutter_app.zip")
            storage.put(
                sources_key, Path(f"{zip_base}.zip").read_bytes(), content_type="application/zip"
            )
            for png in sorted(paths.generated_screens_dir.glob("*.png")):
                key = build_key(job_id=job_id, kind="generated_screenshots", name=png.name)
                storage.put(key, png.read_bytes(), content_type="image/png")
            db.add(
                GenerationResult(
                    job_id=job.id,
                    sources_key=sources_key,
                    compliance_score=report["compliance_score"],
                    selftest_report=report,
                )
            )
            _finish_stage(db, stage_row)

            job.state = JobState.DONE
            db.commit()
            log.info("run_job.done", job_id=job_id)
            return f"job {job_id} done"
        except Exception as exc:
            db.rollback()
            job2 = db.get(Job, job.id)
            if job2 is not None:
                job2.state = JobState.FAILED
                if stage_row is not None:
                    row = db.get(StageTimeline, stage_row.id)
                    if row is not None:
                        row.error = str(exc)[:2000]
                        row.finished_at = utcnow()
                db.commit()
            log.error("run_job.failed", job_id=job_id, error=str(exc))
            raise
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
