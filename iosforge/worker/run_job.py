"""Celery worker: run the MVP vertical for an uploaded Job (SPEC §7 wiring).

Pulls the APK from MinIO, boots the emulator, crawls screens, generates a Flutter
app, and writes stage timeline + artifacts back to the DB/MinIO. Reuses the
phase-1 vertical in ``iosforge.mvp`` and the EPIC-2 infra (DB/Celery/MinIO).
Requires a booted/bootable AVD on the worker host.
"""

from __future__ import annotations

import json
import re
import shutil
import tempfile
import uuid
from collections.abc import Callable
from pathlib import Path

from sqlalchemy import delete, select

from iosforge.common.config import get_settings
from iosforge.common.logging import get_logger
from iosforge.common.queue import PipelineTask, celery_app
from iosforge.common.types import JobState, Stage
from iosforge.db.base import utcnow
from iosforge.db.models import (
    ApkArtifact,
    CodegenTask,
    DataArchiveArtifact,
    GenerationResult,
    Job,
    StageTimeline,
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


def _ingest_appstore(db, storage, job: Job, settings, appstore) -> None:
    """Resolve App Store metadata for an appstore job and record it as reference.

    Screenshots are stored as reference metadata only — they are not yet the
    pipeline screens fed to codegen. Metadata is MERGED into ``source_app_metadata``
    so the ``app_id``/``country`` seeded at creation time are preserved.
    """
    seeded = dict(job.source_app_metadata or {})
    app_id = str(seeded.get("app_id") or "")
    country = str(seeded.get("country") or settings.appstore_country_default)

    meta = appstore.fetch_metadata(
        app_id,
        country,
        api_base=settings.appstore_api_base,
        timeout=float(settings.appstore_fetch_timeout_s),
        max_screenshots=settings.appstore_max_screenshots,
    )
    shots = appstore.download_screenshots(
        meta.screenshot_urls, timeout=float(settings.appstore_fetch_timeout_s)
    )
    screenshot_keys: list[str] = []
    for name, data in shots:
        key = build_key(job_id=str(job.id), kind="appstore_screenshots", name=name)
        storage.put(key, data, content_type="image/png")
        screenshot_keys.append(key)

    job.source_app_metadata = {
        **seeded,
        "track_name": meta.track_name,
        "description": meta.description,
        "seller_name": meta.seller_name,
        "bundle_id": meta.bundle_id,
        "artwork_url": meta.artwork_url,
        "screenshot_urls": meta.screenshot_urls,
        "screenshot_keys": screenshot_keys,
        "fetched_at": utcnow().isoformat(),
    }
    db.commit()


@celery_app.task(base=PipelineTask, name="iosforge.run_job", bind=True)
def run_job(self, job_id: str) -> str:
    from iosforge.mvp import (
        ad_analysis,
        admin_gen,
        admin_provision,
        analyze,
        appstore,
        codegen,
        compliance,
        crawl,
        emulator,
        flutter_wiring,
        frida_ingest,
        screen_filter,
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
        apk_art = db.scalar(select(ApkArtifact).where(ApkArtifact.job_id == job.id))
        is_appstore = job.submission_kind == "appstore"
        if apk_art is None and not is_appstore:
            job.state = JobState.FAILED
            db.commit()
            return f"job {job_id} has no source artifact"

        stage_row: StageTimeline | None = None
        try:
            if is_appstore:
                archive_art = db.scalar(
                    select(DataArchiveArtifact).where(DataArchiveArtifact.job_id == job.id)
                )
                if archive_art is None:
                    job.state = JobState.FAILED
                    db.commit()
                    return f"job {job_id} appstore has no data archive"

                paths = RunPaths.create(tmp / "run")

                # --- Walkthrough: App Store metadata (best-effort) + Frida archive ---
                stage_row = _start_stage(db, job, Stage.WALKTHROUGH, JobState.WALKTHROUGH)
                try:
                    _ingest_appstore(db, storage, job, settings, appstore)
                except Exception as exc:  # metadata is enrichment; the archive is the source
                    log.warning("run_job.appstore_metadata_failed", job_id=job_id, error=str(exc))

                archive_path = tmp / "data_archive"
                archive_path.write_bytes(storage.get(archive_art.storage_key))
                screen_map = frida_ingest.ingest_archive(archive_path, paths)

                screenshot_keys = []
                for png in sorted(paths.screens_dir.glob("*.png")):
                    key = build_key(job_id=job_id, kind="screenshots", name=png.name)
                    storage.put(key, png.read_bytes(), content_type="image/png")
                    screenshot_keys.append(key)
                if paths.network_index_json.exists():
                    storage.put(
                        build_key(job_id=job_id, kind="network_index", name="network_index.json"),
                        paths.network_index_json.read_bytes(),
                        content_type="application/json",
                    )
                db.add(
                    WalkthroughResult(
                        job_id=job.id,
                        screenshot_keys=screenshot_keys,
                        screen_map=screen_map,
                        provider="frida-appstore",
                        strategy={"source": "frida-appstore"},
                    )
                )
                _finish_stage(db, stage_row)
                stage_row = None

                # --- Analysis only: App Spec v3 + handoff; development is a separate,
                # human-triggered step (no codegen here). ---
                stage_row = _start_stage(db, job, Stage.ANALYSIS, JobState.ANALYSIS)
                analyze.analyze(paths)
                for kind, src in (("app_spec", paths.app_spec_json), ("spec_md", paths.spec_md)):
                    if src.exists():
                        ctype = "application/json" if src.suffix == ".json" else "text/markdown"
                        storage.put(
                            build_key(job_id=job_id, kind=kind, name=src.name),
                            src.read_bytes(),
                            content_type=ctype,
                        )
                if paths.handoff_dir.exists():
                    for hf in sorted(paths.handoff_dir.rglob("*")):
                        if hf.is_file():
                            storage.put(
                                build_key(job_id=job_id, kind="handoff", name=hf.name),
                                hf.read_bytes(),
                                content_type="application/octet-stream",
                            )
                _finish_stage(db, stage_row)
                stage_row = None
                if settings.auto_build_frontend:
                    job.state = JobState.CODEGEN
                    db.commit()
                    log.info("run_job.analysis_done_autochain", job_id=job_id)
                    build_frontend.apply_async(args=[job_id], queue="codegen")
                    return f"job {job_id} analysis done -> frontend build queued"
                job.state = JobState.DONE
                db.commit()
                log.info("run_job.appstore_analysis_done", job_id=job_id)
                return f"job {job_id} done (analysis)"

            paths = RunPaths.create(tmp / "run")

            # --- Walkthrough: emulator crawl (legacy APK path; not offered in the UI) ---
            stage_row = _start_stage(db, job, Stage.WALKTHROUGH, JobState.WALKTHROUGH)
            if apk_art is not None:
                apk_path = tmp / "app.apk"
                apk_path.write_bytes(storage.get(apk_art.storage_key))
                emulator.start_emulator(settings.admin_avd)
                emulator.wait_for_boot()
                package = emulator.install_apk(apk_path)
                emulator.launch(package)
                screen_map = crawl.walk(paths, package, settings.walkthrough_max_screens)
                provider = "adb-mvp"
                strategy: dict[str, object] = {}
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
                    prefix = ""
                    if settings.firebase_project_id and not settings.firebase_create_project:
                        slug = re.sub(
                            r"[^a-z0-9]+", "_", str(spec.get("app_name", "app")).lower()
                        ).strip("_")
                        prefix = f"{slug}_" if slug else ""
                    admin_dir = admin_gen.generate_admin(
                        spec,
                        paths.run_dir / "admin",
                        collection_prefix=prefix,
                        project_id=settings.firebase_project_id or "app",
                    )
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
                            if settings.generate_wired_app and pres.get("firebase_config"):
                                wired_dir = flutter_wiring.generate_app(
                                    spec,
                                    paths.run_dir / "wired_app",
                                    project_id=pres["project_id"],
                                    collection_prefix=prefix,
                                    firebase_config=pres["firebase_config"],
                                )
                                wired_zip = shutil.make_archive(
                                    str(tmp / "wired_app"), "zip", str(wired_dir)
                                )
                                storage.put(
                                    build_key(
                                        job_id=job_id, kind="wired_app", name="wired_app.zip"
                                    ),
                                    Path(wired_zip).read_bytes(),
                                    content_type="application/zip",
                                )
                                log.info(
                                    "run_job.wired_app_generated",
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


@celery_app.task(base=PipelineTask, name="iosforge.build_frontend", bind=True)
def build_frontend(self, job_id: str) -> str:
    """Human-triggered frontend codegen from a completed analysis (SPEC §7).

    Hydrates a fresh RunPaths from the stored analysis (``screens.json`` from
    ``WalkthroughResult.screen_map``, screenshots + ``app_spec.json`` from object
    storage), then runs decompose + codegen only — no compliance / backend /
    Firebase (frontend-first). Reuses the selected ``codegen_orchestrator``.
    """
    from iosforge.mvp import (
        analyze,
        codegen,
        codegen_checkpoint,
        compliance,
        frida_ingest,
        github_publish,
    )
    from iosforge.mvp.paths import RunPaths

    settings = get_settings()
    storage = S3ArtifactStorage()
    maker = get_sessionmaker()
    tmp = Path(tempfile.mkdtemp(prefix="iosforge-build-"))

    try:
        with maker() as db:
            job = db.get(Job, uuid.UUID(job_id))
            if job is None:
                return f"job {job_id} not found"
            walk = db.scalar(select(WalkthroughResult).where(WalkthroughResult.job_id == job.id))
            if walk is None or not walk.screen_map:
                job.state = JobState.FAILED
                db.commit()
                return f"job {job_id} has no analysis to build from"
            if db.scalar(select(GenerationResult).where(GenerationResult.job_id == job.id)):
                log.info("build_frontend.skip_existing", job_id=job_id)
                return f"job {job_id} already built"

            stage_row: StageTimeline | None = None
            try:
                paths = RunPaths.create(tmp / "run")
                paths.screens_json.write_text(json.dumps(walk.screen_map, ensure_ascii=False))
                for key in walk.screenshot_keys:
                    (paths.screens_dir / key.rsplit("/", 1)[-1]).write_bytes(storage.get(key))
                app_spec_key = build_key(job_id=job_id, kind="app_spec", name="app_spec.json")
                try:
                    paths.app_spec_json.write_bytes(storage.get(app_spec_key))
                except Exception as exc:
                    job.state = JobState.FAILED
                    db.commit()
                    log.error("build_frontend.no_app_spec", job_id=job_id, error=str(exc))
                    return f"job {job_id} app_spec unavailable"

                archive_art = db.scalar(
                    select(DataArchiveArtifact).where(DataArchiveArtifact.job_id == job.id)
                )
                if archive_art is not None:
                    try:
                        archive_path = tmp / "data_archive"
                        archive_path.write_bytes(storage.get(archive_art.storage_key))
                        frida_ingest.ingest_archive(archive_path, paths)
                        log.info("build_frontend.archive_context_restored", job_id=job_id)
                    except Exception as exc:
                        log.warning(
                            "build_frontend.archive_reingest_failed", job_id=job_id, error=str(exc)
                        )

                stage_row = _start_stage(db, job, Stage.CODEGEN, JobState.CODEGEN)

                # Resumable codegen (claude task runner only): restore the last
                # checkpoint and skip finished tasks, else decompose fresh. A crash /
                # rate-limit resumes from the last finished task on the next attempt.
                completed: set[str] = set()
                on_task_done: Callable[[str], None] | None = None
                resumable = settings.codegen_orchestrator == "claude"
                if resumable:
                    loaded = codegen_checkpoint.load(storage, job_id, paths, work_dir=tmp)
                    if loaded is None:
                        analyze.decompose(paths)
                    else:
                        completed = loaded  # tasks.json + workspace restored; skip decompose

                    def _checkpoint(tid: str) -> None:
                        completed.add(tid)
                        codegen_checkpoint.save(storage, job_id, paths, completed, work_dir=tmp)

                    on_task_done = _checkpoint
                    codegen_checkpoint.save(storage, job_id, paths, completed, work_dir=tmp)
                else:
                    analyze.decompose(paths)

                jid = job.id

                def _on_plan(total: int) -> None:
                    with maker() as pdb:
                        pdb.execute(delete(CodegenTask).where(CodegenTask.job_id == jid))
                        pdb.commit()
                    log.info("build_frontend.plan", job_id=job_id, total=total)

                def _on_task(
                    idx: int, total: int, key: str, title: str, status: str, attempts: int
                ) -> None:
                    with maker() as pdb:
                        row = pdb.scalar(
                            select(CodegenTask).where(
                                CodegenTask.job_id == jid, CodegenTask.idx == idx
                            )
                        )
                        if row is None:
                            pdb.add(
                                CodegenTask(
                                    job_id=jid,
                                    idx=idx,
                                    total=total,
                                    task_key=key,
                                    title=title,
                                    status=status,
                                    attempts=attempts,
                                )
                            )
                        else:
                            row.total, row.task_key, row.title = total, key, title
                            row.status, row.attempts = status, attempts
                        pdb.commit()

                codegen.generate(
                    paths,
                    settings,
                    completed=completed,
                    on_task_done=on_task_done,
                    on_plan=_on_plan,
                    on_task=_on_task,
                )
                if resumable:
                    codegen_checkpoint.clear(storage, job_id)

                # Web screen-similarity verification (non-fatal: never loses the
                # generated frontend if the build/render/judge fails).
                compliance_score: float | None = None
                selftest: dict[str, object] = {"mode": "frontend_only"}
                if settings.verify_frontend_web:
                    try:
                        report = compliance.verify_web(
                            paths,
                            weights=compliance.ComplianceWeights.from_settings(settings),
                            threshold=settings.frontend_verify_threshold,
                            soft_floor=settings.compliance_soft_floor,
                            chromium_bin=settings.chromium_bin,
                            wait_ms=settings.web_render_wait_ms,
                            window=settings.web_render_window,
                        )
                        compliance_score = report.get("compliance_score")
                        selftest = {**report, "mode": "frontend_only", "verify": "web"}
                        for png in sorted(paths.generated_screens_dir.glob("*.png")):
                            storage.put(
                                build_key(
                                    job_id=job_id, kind="generated_screenshots", name=png.name
                                ),
                                png.read_bytes(),
                                content_type="image/png",
                            )
                    except Exception as exc:  # verification must not lose the frontend
                        log.warning("build_frontend.verify_failed", job_id=job_id, error=str(exc))
                        selftest = {
                            "mode": "frontend_only",
                            "verify": "web",
                            "status": "verify_failed",
                            "error": str(exc)[:500],
                        }

                zip_base = tmp / "flutter_app"
                shutil.make_archive(str(zip_base), "zip", str(paths.flutter_app))
                sources_key = build_key(job_id=job_id, kind="sources", name="flutter_app.zip")
                storage.put(
                    sources_key,
                    Path(f"{zip_base}.zip").read_bytes(),
                    content_type="application/zip",
                )
                gen = GenerationResult(
                    job_id=job.id,
                    sources_key=sources_key,
                    compliance_score=compliance_score,
                    selftest_report=selftest,
                )
                db.add(gen)
                _finish_stage(db, stage_row)
                stage_row = None

                # --- GitHub Upload stage: push the project to a new public repo ---
                if settings.github_publish and github_publish.load_token(settings):
                    spec = json.loads(paths.app_spec_json.read_text())
                    gh_stage = _start_stage(db, job, Stage.GITHUB_UPLOAD, JobState.GITHUB_UPLOAD)
                    try:
                        result = github_publish.publish(
                            settings,
                            paths.flutter_app,
                            app_name=str(spec.get("app_name") or ""),
                            description=str(spec.get("one_liner") or ""),
                            fallback_slug=job_id[:8],
                        )
                        gen.github_repo_url = result["url"]
                        log.info("build_frontend.github_pushed", job_id=job_id, url=result["url"])
                        _finish_stage(db, gh_stage)
                    except Exception as exc:  # a failed push must not lose the built app
                        log.error(
                            "build_frontend.github_upload_failed", job_id=job_id, error=str(exc)
                        )
                        row = db.get(StageTimeline, gh_stage.id)
                        if row is not None:
                            row.error = str(exc)[:2000]
                            row.finished_at = utcnow()
                        db.commit()

                job.state = JobState.DONE
                db.commit()
                log.info("build_frontend.done", job_id=job_id, repo=gen.github_repo_url)
                return f"job {job_id} frontend built"
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
                log.error("build_frontend.failed", job_id=job_id, error=str(exc))
                raise
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
