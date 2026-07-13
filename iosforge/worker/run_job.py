"""Celery worker: run the MVP vertical for an uploaded Job (SPEC §7 wiring).

Pulls the APK from MinIO, boots the emulator, crawls screens, generates a Flutter
app, and writes stage timeline + artifacts back to the DB/MinIO. Reuses the
phase-1 vertical in ``iosforge.mvp`` and the EPIC-2 infra (DB/Celery/MinIO).
Requires a booted/bootable AVD on the worker host.
"""

from __future__ import annotations

import datetime as dt
import io
import json
import re
import shutil
import tempfile
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sqlalchemy import delete, select

from iosforge.common.config import Settings, get_settings
from iosforge.common.logging import get_logger
from iosforge.common.queue import PipelineTask, celery_app
from iosforge.common.types import JobState, Stage
from iosforge.db.base import utcnow
from iosforge.db.models import (
    ApkArtifact,
    CodegenTask,
    CodemagicBuild,
    DataArchiveArtifact,
    GenerationResult,
    Job,
    StageTimeline,
    WalkthroughResult,
)
from iosforge.db.session import get_sessionmaker
from iosforge.storage.client import S3ArtifactStorage, build_key

if TYPE_CHECKING:
    from iosforge.mvp.paths import RunPaths

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


def _archive_and_upload_sources(paths: RunPaths, storage, job_id: str, tmp: Path) -> str:
    """Zip ``paths.flutter_app`` and upload it as the canonical ``sources`` object.

    Same archive+put pattern the codegen finalize uses; the key is deterministic per
    job, so re-invoking after the VERIFY loop mutates ``flutter_app`` in place refreshes
    the stored zip to match the corrected tree.
    """
    zip_base = tmp / "flutter_app"
    shutil.make_archive(str(zip_base), "zip", str(paths.flutter_app))
    sources_key = build_key(job_id=job_id, kind="sources", name="flutter_app.zip")
    storage.put(sources_key, Path(f"{zip_base}.zip").read_bytes(), content_type="application/zip")
    return sources_key


def _structural_gap_count(report: dict[str, Any]) -> int:
    structural = report.get("structural")
    if not isinstance(structural, dict):
        return 0
    keys = ("missing_screens", "blank_screens", "dead_links", "missing_edges")
    return sum(len(structural.get(key, [])) for key in keys)


def _run_web_verify(
    db,
    job: Job,
    paths: RunPaths,
    settings: Settings,
    storage,
    job_id: str,
    *,
    hard_gate: bool,
) -> tuple[dict[str, Any], bool]:
    """Run the Stage VERIFY structural-web loop and apply the hard gate.

    Emits a ``Stage.VERIFY``/``JobState.VERIFY`` timeline row, runs
    ``compliance.refine_web_until_complete`` (structural audit + visual judge), uploads
    the generated screenshots and structural report, then:
    - on a clean pass (``stop_reason == "all_closed"``) finishes the stage;
    - on exhaustion with gaps open AND ``hard_gate`` sets the row error and
      ``Job.state = NEEDS_INPUT`` (a human decides ship vs rework);
    - a build/render failure is non-fatal (recorded, stage finished, no gate).

    Returns ``(report, gated)`` where ``gated`` tells the caller to skip DONE.
    """
    from iosforge.mvp import compliance

    bound = log.bind(job_id=job_id, stage="verify")
    stage_row = _start_stage(db, job, Stage.VERIFY, JobState.VERIFY)
    try:
        report = compliance.refine_web_until_complete(
            paths,
            threshold=settings.frontend_verify_threshold,
            soft_floor=settings.compliance_soft_floor,
            max_iterations=settings.compliance_max_iterations,
            weights=compliance.ComplianceWeights.from_settings(settings),
            chromium_bin=settings.chromium_bin,
            wait_ms=settings.web_render_wait_ms,
            window=settings.web_render_window,
            blank_max_bytes=settings.web_blank_max_bytes,
            structural_gate=settings.verify_web_structural,
        )
    except Exception as exc:  # build/render failure must not hard-gate the job
        bound.warning("verify.render_failed", error=str(exc))
        report = {
            "mode": "verify",
            "verify": "web_loop",
            "status": "verify_failed",
            "error": str(exc)[:500],
        }
        _finish_stage(db, stage_row)
        return report, False

    try:
        for png in sorted(paths.generated_screens_dir.glob("*.png")):
            storage.put(
                build_key(job_id=job_id, kind="generated_screenshots", name=png.name),
                png.read_bytes(),
                content_type="image/png",
            )
        storage.put(
            build_key(job_id=job_id, kind="selftest", name="selftest_report.json"),
            json.dumps(report, ensure_ascii=False).encode("utf-8"),
            content_type="application/json",
        )
    except Exception as exc:  # persisting artifacts must not lose the verdict
        bound.warning("verify.upload_failed", error=str(exc))

    passed = report.get("stop_reason") == "all_closed"
    gaps = _structural_gap_count(report)
    gated = hard_gate and not passed
    if gated:
        job.state = JobState.NEEDS_INPUT
        row = db.get(StageTimeline, stage_row.id)
        if row is not None:
            if gaps:
                row.error = f"structural gate not met: {gaps} gaps"
            else:
                score = report.get("compliance_score")
                score_text = f"{float(score):.2f}" if isinstance(score, (int, float)) else "n/a"
                row.error = (
                    f"compliance gate not met (score {score_text} "
                    f"< {settings.frontend_verify_threshold:.2f})"
                )
            row.finished_at = utcnow()
        db.commit()
        bound.warning("verify.gated", gaps=gaps, stop_reason=report.get("stop_reason"))
    else:
        _finish_stage(db, stage_row)
        bound.info("verify.done", passed=passed, gaps=gaps)
    return report, gated


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
        codemagic_integration,
        compliance,
        frida_ingest,
        github_publish,
        revenuecat_provision,
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

                # RevenueCat: provision one app per clone (+ products/entitlement/
                # offering) and inject its public SDK key into codegen. Best-effort —
                # a failure never blocks the build.
                rc_config = revenuecat_provision.provision(
                    json.loads(paths.app_spec_json.read_text()), settings=settings
                )
                if rc_config:
                    paths.rc_config_json.write_text(json.dumps(rc_config, indent=2))
                    log.info(
                        "build_frontend.revenuecat_provisioned",
                        job_id=job_id,
                        app_id=rc_config.get("app_id"),
                        bundle_id=rc_config.get("bundle_id"),
                        products=len(rc_config.get("products", [])),
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
                # generated frontend if the build/render/judge fails). The single-pass
                # check runs inline here; the opt-in structural VERIFY loop runs as its
                # own stage after the GenerationResult is committed (see below).
                compliance_score: float | None = None
                selftest: dict[str, object] = {"mode": "frontend_only"}
                if settings.verify_frontend_web and not settings.pipeline_web_verify_loop:
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

                sources_key = _archive_and_upload_sources(paths, storage, job_id, tmp)
                gen = GenerationResult(
                    job_id=job.id,
                    sources_key=sources_key,
                    compliance_score=compliance_score,
                    selftest_report=selftest,
                )
                db.add(gen)
                _finish_stage(db, stage_row)
                stage_row = None

                # --- Stage VERIFY: opt-in structural-web loop (own stage + hard gate) ---
                verify_gated = False
                if settings.pipeline_web_verify_loop:
                    report, verify_gated = _run_web_verify(
                        db,
                        job,
                        paths,
                        settings,
                        storage,
                        job_id,
                        hard_gate=settings.web_verify_hard_gate,
                    )
                    # The loop mutated flutter_app in place; re-archive so MinIO (and the
                    # tree GitHub pushes) matches the corrected sources — even when gated,
                    # so a human/rework continues from the improved tree, not a stale zip.
                    gen.sources_key = _archive_and_upload_sources(paths, storage, job_id, tmp)
                    gen.compliance_score = report.get("compliance_score")
                    gen.selftest_report = {**report, "mode": "frontend_only", "verify": "web_loop"}
                    db.commit()

                # --- GitHub Upload stage: push the project to a new public repo ---
                gh_result: dict[str, str] | None = None
                if (
                    not verify_gated
                    and settings.github_publish
                    and github_publish.load_token(settings)
                ):
                    spec = json.loads(paths.app_spec_json.read_text())
                    gh_stage = _start_stage(db, job, Stage.GITHUB_UPLOAD, JobState.GITHUB_UPLOAD)
                    try:
                        gh_result = github_publish.publish(
                            settings,
                            paths.flutter_app,
                            app_name=str(spec.get("app_name") or ""),
                            description=str(spec.get("one_liner") or ""),
                            fallback_slug=job_id[:8],
                        )
                        gen.github_repo_url = gh_result["url"]
                        log.info(
                            "build_frontend.github_pushed", job_id=job_id, url=gh_result["url"]
                        )
                        _finish_stage(db, gh_stage)
                    except Exception as exc:  # a failed push must not lose the built app
                        gh_result = None
                        log.error(
                            "build_frontend.github_upload_failed", job_id=job_id, error=str(exc)
                        )
                        row = db.get(StageTimeline, gh_stage.id)
                        if row is not None:
                            row.error = str(exc)[:2000]
                            row.finished_at = utcnow()
                        db.commit()

                # --- CodeMagic Integration stage: connect the repo, add codemagic.yaml ---
                if (
                    gh_result is not None
                    and settings.codemagic_integration
                    and codemagic_integration.load_token(settings)
                ):
                    cm_stage = _start_stage(
                        db, job, Stage.CODEMAGIC_INTEGRATION, JobState.CODEMAGIC_INTEGRATION
                    )
                    last_err: str | None = None
                    for attempt in range(2):
                        try:
                            cm_result = codemagic_integration.integrate(
                                settings,
                                repo_full_name=gh_result["full_name"],
                                repo_html_url=gh_result["url"],
                            )
                            gen.codemagic = cm_result
                            log.info(
                                "build_frontend.codemagic_done",
                                job_id=job_id,
                                app_id=cm_result.get("application_id"),
                            )
                            _finish_stage(db, cm_stage)
                            last_err = None
                            break
                        except Exception as exc:  # setup failure must not lose the built app
                            last_err = str(exc)
                            log.warning(
                                "build_frontend.codemagic_attempt_failed",
                                job_id=job_id,
                                attempt=attempt + 1,
                                error=last_err,
                            )
                    if last_err is not None:
                        log.error("build_frontend.codemagic_failed", job_id=job_id, error=last_err)
                        row = db.get(StageTimeline, cm_stage.id)
                        if row is not None:
                            row.error = last_err[:2000]
                            row.finished_at = utcnow()
                        db.commit()

                # Auto-build: chain the CodeMagic iOS build so the pipeline runs to a
                # downloadable artifact instead of stopping at a manual button.
                auto_build = bool(
                    settings.codemagic_auto_build
                    and not verify_gated
                    and isinstance(gen.codemagic, dict)
                    and gen.codemagic.get("application_id")
                )
                if not verify_gated and not auto_build:
                    job.state = JobState.DONE
                db.commit()
                if auto_build:
                    run_codemagic_build.apply_async(args=[job_id], queue="delivery")
                    log.info("build_frontend.auto_build_enqueued", job_id=job_id)
                log.info(
                    "build_frontend.done",
                    job_id=job_id,
                    repo=gen.github_repo_url,
                    verify_gated=verify_gated,
                    auto_build=auto_build,
                )
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


def _parse_iso(value: object) -> dt.datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


@celery_app.task(base=PipelineTask, name="iosforge.run_codemagic_build", bind=True)
def run_codemagic_build(self, job_id: str) -> str:
    """Trigger a CodeMagic iOS build for a connected job and poll it to completion.

    Uses the existing GitHub repo + CodeMagic application (no analysis/codegen).
    Persists status, logs and artifacts to ``codemagic_builds`` as they arrive so
    the admin can follow the build live.
    """
    import time

    from iosforge.mvp import codemagic_build, codemagic_integration, github_publish

    settings = get_settings()
    maker = get_sessionmaker()
    with maker() as db:
        job = db.get(Job, uuid.UUID(job_id))
        if job is None:
            return f"job {job_id} not found"
        gen = db.scalar(
            select(GenerationResult)
            .where(GenerationResult.job_id == job.id)
            .order_by(GenerationResult.created_at.desc())
        )
        cm_meta = (gen.codemagic if gen else None) or {}
        app_id = str(cm_meta.get("application_id") or "")
        if not app_id:
            return f"job {job_id} is not connected to CodeMagic"
        branch = str(cm_meta.get("default_branch") or "main")
        repo_url = str(cm_meta.get("repository_url") or (gen.github_repo_url if gen else "") or "")
        full_name = repo_url.rstrip("/").split("github.com/")[-1] if repo_url else ""

        token = codemagic_build.resolve_token(settings)
        gh_token = github_publish.load_token(settings)

        # Resolve the workflow id from the EFFECTIVE codemagic.yaml template (rendered
        # locally — reliable even right after a force-push, which the GitHub contents API
        # lags behind). A rework/augment force-push replaces the tree with flutter_app/
        # and drops the separately-committed codemagic.yaml, so also restore it in the
        # repo (best-effort) for the UI / future builds.
        try:
            workflow_id = codemagic_integration.resolved_workflow_id(settings, gh_token, full_name)
        except Exception as exc:
            log.warning("codemagic_build.workflow_lookup_failed", job_id=job_id, error=str(exc))
            workflow_id = "ios-unsigned"
        if gh_token and full_name:
            try:
                if codemagic_integration.ensure_codemagic_yaml(
                    settings, gh_token, full_name, branch
                ):
                    log.info("codemagic_build.codemagic_yaml_restored", job_id=job_id)
            except Exception as exc:
                log.warning(
                    "codemagic_build.codemagic_yaml_restore_failed",
                    job_id=job_id,
                    error=str(exc),
                )

        try:
            build_id = codemagic_build.start_build(
                settings, token, app_id=app_id, workflow_id=workflow_id, branch=branch
            )
        except Exception as exc:
            log.error("codemagic_build.start_failed", job_id=job_id, error=str(exc))
            row = CodemagicBuild(
                job_id=job.id,
                build_id="",
                application_id=app_id,
                workflow_id=workflow_id,
                status="failed",
                branch=branch,
                message=str(exc)[:2000],
                artifacts=[],
            )
            db.add(row)
            db.commit()
            return f"job {job_id} codemagic build failed to start"

        row = CodemagicBuild(
            job_id=job.id,
            build_id=build_id,
            application_id=app_id,
            workflow_id=workflow_id,
            status="queued",
            branch=branch,
            artifacts=[],
        )
        db.add(row)
        db.commit()
        log.info("codemagic_build.started", job_id=job_id, build_id=build_id, workflow=workflow_id)

    # Poll to completion, persisting status/logs/artifacts on every tick.
    deadline = 2700
    waited = 0
    while waited <= deadline:
        try:
            build = codemagic_build.get_build(settings, token, build_id)
            summary = codemagic_build.summarize(build)
            with maker() as db:
                r = db.scalar(select(CodemagicBuild).where(CodemagicBuild.build_id == build_id))
                if r is not None:
                    r.status = summary["status"] or r.status
                    r.build_number = summary["build_number"]
                    r.started_at = summary["started_at"]
                    r.finished_at = summary["finished_at"]
                    r.message = (summary["message"] or "")[:4000] or None
                    r.artifacts = summary["artifacts"]
                    r.log_text = codemagic_build.build_logs(build)[:200000] or None
                    started = _parse_iso(summary["started_at"])
                    finished = _parse_iso(summary["finished_at"])
                    if started and finished:
                        r.duration_ms = int((finished - started).total_seconds() * 1000)
                    db.commit()
            if summary["status"] in codemagic_build.TERMINAL:
                log.info(
                    "codemagic_build.done",
                    job_id=job_id,
                    build_id=build_id,
                    status=summary["status"],
                    artifacts=len(summary["artifacts"]),
                )
                return f"job {job_id} codemagic build {summary['status']}"
        except Exception as exc:
            log.warning("codemagic_build.poll_failed", job_id=job_id, error=str(exc))
        time.sleep(8)
        waited += 8
    log.warning("codemagic_build.poll_timeout", job_id=job_id, build_id=build_id)
    return f"job {job_id} codemagic build poll timed out"


@celery_app.task(base=PipelineTask, name="iosforge.rework_frontend", bind=True)
def rework_frontend(self, job_id: str, instructions: str = "", augment: bool = False) -> str:
    """Re-run over an already-built app: edit in place, re-push, re-build.

    Hydrates the existing ``flutter_app`` (+ archive context, app_spec, screens) from
    storage, then either applies ``instructions`` (user rework) or, when ``augment``,
    provisions RevenueCat and runs an incremental gap-analysis pass that ADDS what is
    missing vs the spec (NOT a from-scratch rebuild). Saves a new version, force-pushes
    a fresh commit to the SAME GitHub repo and re-triggers the CodeMagic build.
    """
    import zipfile

    from iosforge.mvp import (
        claude_gen,
        compliance,
        frida_ingest,
        github_publish,
        revenuecat_provision,
    )
    from iosforge.mvp.paths import RunPaths

    settings = get_settings()
    storage = S3ArtifactStorage()
    maker = get_sessionmaker()
    tmp = Path(tempfile.mkdtemp(prefix="iosforge-rework-"))
    try:
        with maker() as db:
            job = db.get(Job, uuid.UUID(job_id))
            if job is None:
                return f"job {job_id} not found"
            walk = db.scalar(select(WalkthroughResult).where(WalkthroughResult.job_id == job.id))
            gen = db.scalar(
                select(GenerationResult)
                .where(GenerationResult.job_id == job.id)
                .order_by(GenerationResult.created_at.desc())
            )
            if walk is None or gen is None:
                return f"job {job_id} has nothing to rework"
            repo_url = gen.github_repo_url or ""
            full_name = repo_url.rstrip("/").split("github.com/")[-1] if repo_url else ""
            prior_codemagic = dict(gen.codemagic or {})

            stage_row: StageTimeline | None = None
            try:
                stage_row = _start_stage(db, job, Stage.CODEGEN, JobState.CODEGEN)
                paths = RunPaths.create(tmp / "run")
                paths.screens_json.write_text(json.dumps(walk.screen_map, ensure_ascii=False))
                for key in walk.screenshot_keys:
                    (paths.screens_dir / key.rsplit("/", 1)[-1]).write_bytes(storage.get(key))
                paths.app_spec_json.write_bytes(
                    storage.get(build_key(job_id=job_id, kind="app_spec", name="app_spec.json"))
                )
                archive_art = db.scalar(
                    select(DataArchiveArtifact).where(DataArchiveArtifact.job_id == job.id)
                )
                if archive_art is not None:
                    try:
                        ap = tmp / "data_archive"
                        ap.write_bytes(storage.get(archive_art.storage_key))
                        frida_ingest.ingest_archive(ap, paths)
                    except Exception as exc:
                        log.warning("rework.archive_reingest_failed", job_id=job_id, error=str(exc))

                paths.flutter_app.parent.mkdir(parents=True, exist_ok=True)
                with zipfile.ZipFile(io.BytesIO(storage.get(gen.sources_key))) as z:
                    z.extractall(paths.flutter_app)

                mode = "augment" if augment else "rework"
                if augment:
                    rc_config = revenuecat_provision.provision(
                        json.loads(paths.app_spec_json.read_text()), settings=settings
                    )
                    if rc_config:
                        paths.rc_config_json.write_text(json.dumps(rc_config, indent=2))
                        log.info(
                            "augment.revenuecat_provisioned",
                            job_id=job_id,
                            app_id=rc_config.get("app_id"),
                            mode=rc_config.get("mode"),
                        )
                    claude_gen.augment(paths)
                else:
                    claude_gen.rework(paths, instructions)

                # Single-pass verify_web (not the Stage VERIFY loop) by design.
                selftest: dict[str, object] = {"mode": mode}
                compliance_score: float | None = None
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
                        selftest = {**report, "mode": mode, "verify": "web"}
                    except Exception as exc:
                        log.warning("rework.verify_failed", job_id=job_id, error=str(exc))

                zip_base = tmp / "flutter_app"
                shutil.make_archive(str(zip_base), "zip", str(paths.flutter_app))
                sources_key = build_key(job_id=job_id, kind="sources", name="flutter_app.zip")
                storage.put(
                    sources_key,
                    Path(f"{zip_base}.zip").read_bytes(),
                    content_type="application/zip",
                )
                job.result_version = (job.result_version or 1) + 1
                final_version = job.result_version
                new_gen = GenerationResult(
                    job_id=job.id,
                    sources_key=sources_key,
                    compliance_score=compliance_score,
                    selftest_report=selftest,
                    github_repo_url=repo_url or None,
                    codemagic=prior_codemagic,
                )
                db.add(new_gen)
                _finish_stage(db, stage_row)
                stage_row = None

                published = False
                if full_name and github_publish.load_token(settings):
                    gh_stage = _start_stage(db, job, Stage.GITHUB_UPLOAD, JobState.GITHUB_UPLOAD)
                    try:
                        github_publish.push_existing(
                            settings,
                            paths.flutter_app,
                            full_name=full_name,
                            message=f"{mode.capitalize()} v{job.result_version}",
                        )
                        _finish_stage(db, gh_stage)
                        published = True
                    except Exception as exc:
                        log.error("rework.github_push_failed", job_id=job_id, error=str(exc))
                        row = db.get(StageTimeline, gh_stage.id)
                        if row is not None:
                            row.error = str(exc)[:2000]
                            row.finished_at = utcnow()
                        db.commit()

                job.state = JobState.DONE
                db.commit()
                log.info("rework.done", job_id=job_id, version=job.result_version)
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
                log.error("rework.failed", job_id=job_id, error=str(exc))
                raise

    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    if published and prior_codemagic.get("application_id"):
        run_codemagic_build.apply_async(args=[job_id], queue="delivery")
    return f"job {job_id} reworked (v{final_version})"


@celery_app.task(base=PipelineTask, name="iosforge.reverify_web", bind=True)
def reverify_web(self, job_id: str) -> str:
    """Re-run the Stage VERIFY structural-web loop on an already-built app.

    Hydrates the run_dir from storage exactly like :func:`rework_frontend`
    (``screens.json`` from ``WalkthroughResult.screen_map``, screenshots from
    ``walk.screenshot_keys``, ``flutter_app`` from ``gen.sources_key`` zip), runs the
    structural loop via :func:`_run_web_verify`, then writes a NEW ``GenerationResult``
    (bumping ``result_version``) carrying the structural report. The final Job state is
    ``DONE`` on a clean pass, or ``NEEDS_INPUT`` when the hard gate holds open gaps.
    """
    import zipfile

    from iosforge.mvp import frida_ingest, github_publish
    from iosforge.mvp.paths import RunPaths

    settings = get_settings()
    storage = S3ArtifactStorage()
    maker = get_sessionmaker()
    tmp = Path(tempfile.mkdtemp(prefix="iosforge-verify-"))
    final_version = 0
    gated = True
    published = False
    prior_codemagic: dict[str, Any] = {}
    try:
        with maker() as db:
            job = db.get(Job, uuid.UUID(job_id))
            if job is None:
                return f"job {job_id} not found"
            walk = db.scalar(select(WalkthroughResult).where(WalkthroughResult.job_id == job.id))
            gen = db.scalar(
                select(GenerationResult)
                .where(GenerationResult.job_id == job.id)
                .order_by(GenerationResult.created_at.desc())
            )
            if walk is None or gen is None or not walk.screen_map:
                return f"job {job_id} has nothing to verify"
            repo_url = gen.github_repo_url or ""
            full_name = repo_url.rstrip("/").split("github.com/")[-1] if repo_url else ""
            prior_codemagic = dict(gen.codemagic or {})

            stage_row: StageTimeline | None = None
            try:
                paths = RunPaths.create(tmp / "run")
                paths.screens_json.write_text(json.dumps(walk.screen_map, ensure_ascii=False))
                for key in walk.screenshot_keys:
                    (paths.screens_dir / key.rsplit("/", 1)[-1]).write_bytes(storage.get(key))
                try:
                    paths.app_spec_json.write_bytes(
                        storage.get(build_key(job_id=job_id, kind="app_spec", name="app_spec.json"))
                    )
                except Exception as exc:
                    log.warning("reverify.no_app_spec", job_id=job_id, error=str(exc))

                archive_art = db.scalar(
                    select(DataArchiveArtifact).where(DataArchiveArtifact.job_id == job.id)
                )
                if archive_art is not None:
                    try:
                        ap = tmp / "data_archive"
                        ap.write_bytes(storage.get(archive_art.storage_key))
                        frida_ingest.ingest_archive(ap, paths)
                    except Exception as exc:
                        log.warning(
                            "reverify.archive_reingest_failed", job_id=job_id, error=str(exc)
                        )

                paths.flutter_app.parent.mkdir(parents=True, exist_ok=True)
                with zipfile.ZipFile(io.BytesIO(storage.get(gen.sources_key))) as z:
                    z.extractall(paths.flutter_app)

                report, gated = _run_web_verify(
                    db,
                    job,
                    paths,
                    settings,
                    storage,
                    job_id,
                    hard_gate=settings.web_verify_hard_gate,
                )

                # The loop mutated flutter_app in place; persist the corrected tree so the
                # new version (and any later rework/reverify) continues from it, not the
                # stale zip — done unconditionally, including when gated to NEEDS_INPUT.
                new_sources_key = _archive_and_upload_sources(paths, storage, job_id, tmp)
                job.result_version = (job.result_version or 1) + 1
                final_version = job.result_version
                new_gen = GenerationResult(
                    job_id=job.id,
                    sources_key=new_sources_key,
                    compliance_score=report.get("compliance_score"),
                    selftest_report={**report, "mode": "reverify", "verify": "web_loop"},
                    github_repo_url=repo_url or None,
                    codemagic=prior_codemagic,
                )
                db.add(new_gen)
                db.commit()

                # Publish only on a clean pass — never deliver a known-incomplete build.
                if gated:
                    log.info("reverify.gated_skip_publish", job_id=job_id, version=final_version)
                else:
                    if full_name and github_publish.load_token(settings):
                        gh_stage = _start_stage(
                            db, job, Stage.GITHUB_UPLOAD, JobState.GITHUB_UPLOAD
                        )
                        try:
                            github_publish.push_existing(
                                settings,
                                paths.flutter_app,
                                full_name=full_name,
                                message=f"Verify v{job.result_version}",
                            )
                            _finish_stage(db, gh_stage)
                            published = True
                        except Exception as exc:  # a failed push must not lose the version
                            log.error("reverify.github_push_failed", job_id=job_id, error=str(exc))
                            row = db.get(StageTimeline, gh_stage.id)
                            if row is not None:
                                row.error = str(exc)[:2000]
                                row.finished_at = utcnow()
                            db.commit()
                    job.state = JobState.DONE
                    db.commit()

                log.info("reverify.done", job_id=job_id, version=final_version, gated=gated)
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
                log.error("reverify.failed", job_id=job_id, error=str(exc))
                raise
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    if not gated and published and prior_codemagic.get("application_id"):
        run_codemagic_build.apply_async(args=[job_id], queue="delivery")
    return f"job {job_id} reverified (v{final_version})"
