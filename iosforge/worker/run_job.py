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
from iosforge.db.models import (
    ApkArtifact,
    GenerationResult,
    Job,
    StageTimeline,
    VideoArtifact,
    WalkthroughResult,
)
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
    from iosforge.mvp import (
        ad_analysis,
        admin_gen,
        admin_provision,
        analyze,
        codegen,
        compliance,
        crawl,
        emulator,
        screen_filter,
        video_frames,
    )
    from iosforge.mvp.paths import RunPaths

    settings = get_settings()
    storage = S3ArtifactStorage()
    maker = get_sessionmaker()
    tmp = Path(tempfile.mkdtemp(prefix="iosforge-job-"))

    with maker() as db:
        job = db.get(Job, uuid.UUID(job_id))
        if job is None:
            return f"job {job_id} not found"
        video_art = db.scalar(select(VideoArtifact).where(VideoArtifact.job_id == job.id))
        apk_art = db.scalar(select(ApkArtifact).where(ApkArtifact.job_id == job.id))
        if video_art is None and apk_art is None:
            job.state = JobState.FAILED
            db.commit()
            return f"job {job_id} has no source artifact"

        stage_row: StageTimeline | None = None
        try:
            paths = RunPaths.create(tmp / "run")

            # --- Walkthrough: screens from a video upload or an emulator crawl ---
            stage_row = _start_stage(db, job, Stage.WALKTHROUGH, JobState.WALKTHROUGH)
            if video_art is not None:
                video_path = tmp / "recording"
                video_path.write_bytes(storage.get(video_art.storage_key))
                screen_map = video_frames.extract_frames(
                    video_path,
                    paths,
                    fps=settings.video_frame_fps,
                    dedup=settings.video_dedup,
                    max_frames=settings.video_max_frames,
                )
                provider = "ffmpeg-video"
                strategy: dict[str, object] = {
                    "fps": settings.video_frame_fps,
                    "dedup": settings.video_dedup,
                    "max_frames": settings.video_max_frames,
                }
            elif apk_art is not None:
                apk_path = tmp / "app.apk"
                apk_path.write_bytes(storage.get(apk_art.storage_key))
                emulator.start_emulator(settings.admin_avd)
                emulator.wait_for_boot()
                package = emulator.install_apk(apk_path)
                emulator.launch(package)
                screen_map = crawl.walk(paths, package, settings.walkthrough_max_screens)
                provider = "adb-mvp"
                strategy = {}
            else:  # guarded above; keeps the type-checker happy
                raise RuntimeError("no source artifact")

            screenshot_keys = []
            for png in sorted(paths.screens_dir.glob("*.png")):
                key = build_key(job_id=job_id, kind="screenshots", name=png.name)
                storage.put(key, png.read_bytes(), content_type="image/png")
                screenshot_keys.append(key)
            db.add(
                WalkthroughResult(
                    job_id=job.id,
                    screenshot_keys=screenshot_keys,
                    screen_map=screen_map,
                    provider=provider,
                    strategy=strategy,
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
            if settings.filter_junk_frames:
                audit = screen_filter.filter_screens(paths)
                labels_key = build_key(
                    job_id=job_id, kind="screen_labels", name="screen_labels.json"
                )
                storage.put(
                    labels_key,
                    json.dumps(audit).encode(),
                    content_type="application/json",
                )
            analyze.analyze(paths)

            if settings.analyze_ads and paths.ad_screens_dir.exists():
                for png in sorted(paths.ad_screens_dir.glob("*.png")):
                    storage.put(
                        build_key(job_id=job_id, kind="ad_frames", name=png.name),
                        png.read_bytes(),
                        content_type="image/png",
                    )
                ad_res = ad_analysis.analyze_ads(paths)
                if ad_res is not None:
                    storage.put(
                        build_key(job_id=job_id, kind="ad_analysis", name="ad_analysis.json"),
                        ad_res.read_bytes(),
                        content_type="application/json",
                    )

            if settings.generate_admin:
                spec = json.loads(paths.app_spec_json.read_text())
                if spec.get("backend", {}).get("admin_panel_needed"):
                    admin_dir = admin_gen.generate_admin(spec, paths.run_dir / "admin")
                    admin_zip = shutil.make_archive(str(tmp / "admin"), "zip", str(admin_dir))
                    storage.put(
                        build_key(job_id=job_id, kind="admin", name="admin.zip"),
                        Path(admin_zip).read_bytes(),
                        content_type="application/zip",
                    )
                    storage.put(
                        build_key(job_id=job_id, kind="admin", name="manifest.json"),
                        (admin_dir / "manifest.json").read_bytes(),
                        content_type="application/json",
                    )
                    if settings.provision_admin and settings.firebase_sa_path:
                        try:
                            pres = admin_provision.provision(admin_dir, settings)
                            storage.put(
                                build_key(
                                    job_id=job_id, kind="admin", name="provision_result.json"
                                ),
                                json.dumps(pres).encode(),
                                content_type="application/json",
                            )
                            log.info(
                                "run_job.admin_provisioned",
                                job_id=job_id,
                                project=pres["project_id"],
                            )
                        except Exception as exc:  # provisioning must not fail the job
                            log.error(
                                "run_job.admin_provision_failed", job_id=job_id, error=str(exc)
                            )

            if settings.pipeline_stop_after_analyze:
                for kind, src in (("app_spec", paths.app_spec_json), ("spec_md", paths.spec_md)):
                    if src.exists():
                        ctype = "application/json" if src.suffix == ".json" else "text/markdown"
                        storage.put(
                            build_key(job_id=job_id, kind=kind, name=src.name),
                            src.read_bytes(),
                            content_type=ctype,
                        )
                _finish_stage(db, stage_row)
                job.state = JobState.DONE
                db.commit()
                log.info("run_job.analysis_only_done", job_id=job_id)
                return f"job {job_id} done (analysis only)"

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
