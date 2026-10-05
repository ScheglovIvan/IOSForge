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
    from iosforge.mvp.legal_pages import DataPractice
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


def _store_signing(job: Job, settings: Settings) -> Any:
    """Signed App Store build inputs for a store-profile job, or None.

    Returns a ``StoreSigning`` only when the job asks for a store build AND both the app's
    numeric App Store id (per job) and an uploaded App Store Connect signing credential
    (admin Signing settings page → secrets/) are present. The key itself reaches the build
    as injected environment variables, so no name registered in the CodeMagic UI is needed.
    Otherwise None → the unsigned sideload workflow is used.
    """
    from iosforge.mvp import asc_credentials
    from iosforge.mvp.codemagic_integration import StoreSigning

    meta = job.source_app_metadata or {}
    if str(meta.get("build_profile") or "") != "store":
        return None
    apple_id = str(meta.get("appstore_apple_id") or "").strip()
    if not apple_id or not asc_credentials.is_configured(settings, str(job.id)):
        return None
    key_name = str(
        meta.get("asc_api_key_name") or settings.codemagic_asc_api_key_name or ""
    ).strip()
    return StoreSigning(asc_api_key_name=key_name, apple_id=apple_id)


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
        apphud_provision,
        attribution,
        build_profile,
        claude_gen,
        codegen,
        codegen_checkpoint,
        codemagic_integration,
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

                # Build profile (test/real) → bundle id + display name + store mode.
                spec_data = json.loads(paths.app_spec_json.read_text())
                ident = build_profile.resolve_identity(job.source_app_metadata, spec_data, settings)
                log.info(
                    "build_frontend.identity",
                    job_id=job_id,
                    profile=ident.profile,
                    bundle_id=ident.bundle_id,
                    app_name=ident.app_name,
                )

                # Apphud: derive the product ids + placement for this clone and inject
                # the public SDK key into codegen (Apphud has no provisioning API — the
                # dashboard owns app/products). Best-effort — never blocks the build.
                apphud_config = apphud_provision.provision(
                    spec_data,
                    settings=settings,
                    bundle_id=ident.bundle_id,
                    app_name=ident.app_name,
                    sandbox=ident.sandbox,
                    api_key=(job.source_app_metadata or {}).get("apphud_api_key"),
                )
                if apphud_config:
                    paths.apphud_config_json.write_text(json.dumps(apphud_config, indent=2))
                    log.info(
                        "build_frontend.apphud_configured",
                        job_id=job_id,
                        bundle_id=apphud_config.get("bundle_id"),
                        mode=apphud_config.get("mode"),
                        products=len(apphud_config.get("products", [])),
                    )

                # Attribution (Tenjin): only the SDK key is injected — traffic sources are
                # connected in the Tenjin dashboard, so a new source needs no rebuild.
                attribution_config = attribution.provision(
                    settings=settings,
                    api_key=(job.source_app_metadata or {}).get("tenjin_api_key"),
                )
                if attribution_config:
                    paths.attribution_config_json.write_text(
                        json.dumps(attribution_config, indent=2)
                    )
                    log.info(
                        "build_frontend.attribution_configured",
                        job_id=job_id,
                        provider=attribution_config.get("provider"),
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

                # Compile gate: `flutter analyze` + bounded fix loop so non-compiling code
                # never reaches GitHub / the CodeMagic iOS build.
                if settings.codegen_compile_gate:
                    remaining = claude_gen.ensure_compiles(
                        paths, attempts=settings.codegen_compile_gate_attempts
                    )
                    if remaining:
                        log.warning(
                            "build_frontend.compile_gate_unresolved",
                            job_id=job_id,
                            count=len(remaining),
                            sample=remaining[:5],
                        )

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

                # ATT copy + the SKAdNetwork list must ride along into the repo so the
                # build merges them into Info.plist (a missing network id = no SKAN).
                if attribution_config:
                    attribution.stage_ios_assets(settings, paths.flutter_app, attribution_config)
                from iosforge.mvp import ios_compliance

                ios_compliance.stage(
                    paths.flutter_app,
                    bool((job.source_app_metadata or {}).get("export_compliance_exempt")),
                )
                _stage_app_icon(job_id, storage, paths.flutter_app)
                _stage_audio_envelopes(paths.flutter_app)

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
                                bundle_id=ident.bundle_id,
                                app_name=ident.app_name,
                                signing=_store_signing(job, settings),
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

    from iosforge.mvp import build_profile, codemagic_build, codemagic_integration, github_publish

    settings = get_settings()
    storage = S3ArtifactStorage()
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

        # Build profile identity (bundle id + display name) — from the current job
        # metadata, so admin-changed settings reach the rebuild.
        try:
            spec_data = json.loads(
                storage.get(build_key(job_id=job_id, kind="app_spec", name="app_spec.json"))
            )
        except Exception:
            spec_data = {}
        ident = build_profile.resolve_identity(job.source_app_metadata, spec_data, settings)

        token = codemagic_build.resolve_token(settings)
        gh_token = github_publish.load_token(settings)

        # Resolve the workflow id from the EFFECTIVE codemagic.yaml template (rendered
        # locally — reliable even right after a force-push, which the GitHub contents API
        # lags behind). A rework/augment force-push replaces the tree with flutter_app/
        # and drops the separately-committed codemagic.yaml, so also restore it in the
        # repo (best-effort) for the UI / future builds.
        signing = _store_signing(job, settings)
        try:
            workflow_id = codemagic_integration.resolved_workflow_id(
                settings,
                gh_token,
                full_name,
                bundle_id=ident.bundle_id,
                app_name=ident.app_name,
                signing=signing,
            )
        except Exception as exc:
            log.warning("codemagic_build.workflow_lookup_failed", job_id=job_id, error=str(exc))
            workflow_id = "ios-store" if signing else "ios-unsigned"
        if gh_token and full_name:
            try:
                if codemagic_integration.ensure_codemagic_yaml(
                    settings,
                    gh_token,
                    full_name,
                    branch,
                    bundle_id=ident.bundle_id,
                    app_name=ident.app_name,
                    signing=signing,
                ):
                    log.info(
                        "codemagic_build.codemagic_yaml_updated",
                        job_id=job_id,
                        bundle_id=ident.bundle_id,
                    )
            except Exception as exc:
                log.warning(
                    "codemagic_build.codemagic_yaml_restore_failed",
                    job_id=job_id,
                    error=str(exc),
                )

        build_env: dict[str, str] | None = None
        if signing is not None:
            from iosforge.mvp import asc_credentials

            build_env = asc_credentials.build_env(settings, job_id)
        try:
            build_id = codemagic_build.start_build(
                settings,
                token,
                app_id=app_id,
                workflow_id=workflow_id,
                branch=branch,
                env=build_env,
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
    emits the Apphud config and runs an incremental gap-analysis pass that ADDS what is
    missing vs the spec (NOT a from-scratch rebuild). Saves a new version, force-pushes
    a fresh commit to the SAME GitHub repo and re-triggers the CodeMagic build.
    """
    import zipfile

    from iosforge.mvp import (
        apphud_provision,
        attribution,
        build_profile,
        claude_gen,
        compliance,
        frida_ingest,
        github_publish,
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
                # Emit apphud_config.json for BOTH modes: without it in the workspace the
                # codegen agent skips Apphud (the _RC_NOTE is gated on that file) and
                # reverts the paywall to a local stand-in, so real sandbox purchases stop
                # working after a plain rework.
                spec_data = json.loads(paths.app_spec_json.read_text())
                ident = build_profile.resolve_identity(job.source_app_metadata, spec_data, settings)
                apphud_config = apphud_provision.provision(
                    spec_data,
                    settings=settings,
                    bundle_id=ident.bundle_id,
                    app_name=ident.app_name,
                    sandbox=ident.sandbox,
                    api_key=(job.source_app_metadata or {}).get("apphud_api_key"),
                )
                if apphud_config:
                    paths.apphud_config_json.write_text(json.dumps(apphud_config, indent=2))
                    log.info(
                        "rework.apphud_configured",
                        job_id=job_id,
                        mode=mode,
                        bundle_id=apphud_config.get("bundle_id"),
                        store_mode=apphud_config.get("mode"),
                    )

                attribution_config = attribution.provision(
                    settings=settings,
                    api_key=(job.source_app_metadata or {}).get("tenjin_api_key"),
                )
                if attribution_config:
                    paths.attribution_config_json.write_text(
                        json.dumps(attribution_config, indent=2)
                    )
                    log.info(
                        "rework.attribution_configured",
                        job_id=job_id,
                        mode=mode,
                        provider=attribution_config.get("provider"),
                    )
                if augment:
                    claude_gen.augment(paths, timeout=settings.codegen_rework_timeout_s)
                else:
                    claude_gen.rework(
                        paths, instructions, timeout=settings.codegen_rework_timeout_s
                    )

                if settings.codegen_compile_gate:
                    remaining = claude_gen.ensure_compiles(
                        paths,
                        attempts=settings.codegen_compile_gate_attempts,
                        timeout=settings.codegen_rework_timeout_s,
                    )
                    if remaining:
                        log.warning(
                            "rework_frontend.compile_gate_unresolved",
                            job_id=job_id,
                            count=len(remaining),
                            sample=remaining[:5],
                        )

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

                # Staged before the archive so the snapshot (and every later rework that
                # hydrates from it) keeps the ATT copy + SKAdNetwork list.
                if attribution_config:
                    attribution.stage_ios_assets(settings, paths.flutter_app, attribution_config)
                from iosforge.mvp import ios_compliance

                ios_compliance.stage(
                    paths.flutter_app,
                    bool((job.source_app_metadata or {}).get("export_compliance_exempt")),
                )
                _stage_app_icon(job_id, storage, paths.flutter_app)
                _stage_audio_envelopes(paths.flutter_app)

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


def _stage_audio_envelopes(flutter_app: Path) -> bool:
    """Measure the app's audio and write the envelopes it draws its waveform from.

    Done at build time from the actual clips: a hand-written envelope looks varied
    but repeats, which shows up on screen as identical clumps of bars.
    """
    from iosforge.mvp import audio_envelope

    audio_dir = flutter_app / "assets" / "audio"
    if not audio_dir.is_dir():
        return False
    envelopes = audio_envelope.measure_directory(audio_dir)
    if not envelopes:
        return False
    target = flutter_app / "lib" / "core" / "audio" / "sound_envelopes.g.dart"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(audio_envelope.to_dart(envelopes), encoding="utf-8")
    log.info("audio_envelope.staged", clips=len(envelopes))
    return True


def _slide_contract(storage, job_id: str, index: int, spec: dict[str, Any]) -> dict[str, Any]:
    """The analysed contract for one slide, or a minimal stand-in.

    A contract records what the source slide sells and which elements must not be
    lost — the product of a full analysis pass. When that pass has not run, the
    model still SEES the source image, so a slide can be drawn from the app's own
    description instead of blocking on an analysis that costs half an hour.
    """
    try:
        stored = storage.get(
            build_key(job_id=job_id, kind="store_assets", name="slide_contracts.json")
        )
        for entry in json.loads(stored):
            if int(entry.get("index", 0)) == index:
                return dict(entry)
    except Exception:
        pass

    log.info("store_slide.contract_fallback", job_id=job_id, index=index)
    sells = str(spec.get("one_liner") or spec.get("description") or "").strip()
    return {
        "index": index,
        "sells": sells[:220] or "what this app does, as the source slide presents it",
        "device": {"treatment": "as_shown"},
    }


def _stage_app_icon(job_id: str, storage, flutter_app: Path) -> bool:
    """Carry the chosen icon into the project being built, when one exists.

    The icon is generated and re-rolled independently of the app, so the build
    reads whichever version is current at the time it runs. Absence is normal —
    a job whose icon was never generated builds with the Flutter default.
    """
    from iosforge.mvp import app_icon, icon_stage

    key = build_key(job_id=job_id, kind="app_icon", name=app_icon.ICON_NAME)
    try:
        if not storage.exists(key):
            return False
        return icon_stage.stage(flutter_app, storage.get(key))
    except Exception as exc:
        log.warning("icon_stage.failed", job_id=job_id, error=str(exc))
        return False


def _app_palette(job_id: str, storage) -> dict[str, str]:
    """The palette the generated app actually compiled, or empty when unreadable.

    Read from the sources archive rather than the spec: only the theme file states
    what the shipped app looks like. Any failure degrades to the spec's tokens
    instead of failing the job.
    """
    import zipfile

    from iosforge.mvp import app_icon

    maker = get_sessionmaker()
    try:
        with maker() as db:
            gen = db.scalar(
                select(GenerationResult)
                .where(GenerationResult.job_id == uuid.UUID(job_id))
                .order_by(GenerationResult.created_at.desc())
            )
            key = gen.sources_key if gen else ""
        if not key:
            return {}
        with zipfile.ZipFile(io.BytesIO(storage.get(key))) as archive:
            for name in archive.namelist():
                if name.endswith("app_colors.dart"):
                    palette = app_icon.palette_from_app(
                        archive.read(name).decode("utf-8", "replace")
                    )
                    if palette:
                        log.info("app_icon.palette_from_app", job_id=job_id, **palette)
                        return palette
    except Exception as exc:
        log.warning("app_icon.palette_read_failed", job_id=job_id, error=str(exc))
    return {}


@celery_app.task(base=PipelineTask, name="iosforge.publish_legal_pages", bind=True)
def publish_legal_pages(
    self, job_id: str, contact_email: str = "", app_name_override: str = ""
) -> str:
    """Publish the app's Privacy Policy to GitHub Pages and record the URL.

    App Store Connect will not accept a submission without a Privacy Policy URL, and
    review rejects a paywall whose Terms/Privacy links do nothing. The page is built
    from what this build actually declares — the SDKs in its pubspec and the usage
    descriptions in ``ios_permissions.json`` — and pushed to a ``gh-pages`` branch
    the code push never touches, so re-generating the app cannot delete it.
    """
    import zipfile

    from iosforge.mvp import github_publish, legal_pages

    settings = get_settings()
    storage = S3ArtifactStorage()
    maker = get_sessionmaker()
    tmp = Path(tempfile.mkdtemp(prefix="iosforge-legal-"))
    try:
        with maker() as db:
            job = db.get(Job, uuid.UUID(job_id))
            if job is None:
                return f"job {job_id} not found"
            gen = db.scalar(select(GenerationResult).where(GenerationResult.job_id == job.id))
            repo_url = (gen.github_repo_url if gen else "") or ""
        if not repo_url:
            return f"job {job_id}: no GitHub repo to publish to"

        spec = json.loads(
            storage.get(build_key(job_id=job_id, kind="app_spec", name="app_spec.json"))
        )
        app_name = app_name_override or str(spec.get("app_name") or spec.get("name") or "This app")
        bundle_id = str(spec.get("bundle_id") or "")

        sources = tmp / "flutter_app.zip"
        sources.write_bytes(
            storage.get(build_key(job_id=job_id, kind="sources", name="flutter_app.zip"))
        )
        pubspec = ""
        permissions: dict[str, object] = {}
        with zipfile.ZipFile(sources) as archive:
            for name in archive.namelist():
                if name == "pubspec.yaml":
                    pubspec = archive.read(name).decode("utf-8", "replace")
                elif name == "ios_permissions.json":
                    try:
                        permissions = json.loads(archive.read(name))
                    except json.JSONDecodeError:
                        permissions = {}
                if not bundle_id and name == "ios_permissions.json":
                    continue

        facts = legal_pages.facts_from_build(
            app_name=app_name,
            bundle_id=bundle_id,
            contact_email=contact_email or settings.legal_contact_email,
            pubspec=pubspec,
            permissions=permissions,
            remote_endpoints=_remote_endpoints(pubspec, spec),
        )
        url = legal_pages.publish_pages(
            settings=settings,
            token=github_publish.load_token(settings),
            repo_full_name=legal_pages.repo_full_name(repo_url),
            files=legal_pages.build_pages(facts),
            commit_message=f"Publish privacy policy for {app_name}",
        )
        privacy_url = f"{url}/privacy"
        support_url = f"{url}/support"
        storage.put(
            build_key(job_id=job_id, kind="legal", name="urls.json"),
            json.dumps(
                {
                    "privacy_policy_url": privacy_url,
                    "terms_url": legal_pages.APPLE_EULA_URL,
                    "support_url": support_url,
                },
                indent=2,
            ).encode("utf-8"),
            content_type="application/json",
        )
        log.info("legal_pages.recorded", job_id=job_id, url=privacy_url)
        return privacy_url
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _remote_endpoints(pubspec: str, spec: dict[str, Any]) -> list[DataPractice]:
    """Third-party endpoints the app sends user input to, as policy disclosures."""
    from iosforge.mvp.legal_pages import DataPractice as _Practice

    out: list[DataPractice] = []
    blob = json.dumps(spec, ensure_ascii=False).lower()
    if "vin" in blob and "http" in pubspec:
        out.append(
            _Practice(
                "Vehicle lookup",
                "When you look up a VIN, that number is sent to the United States "
                "National Highway Traffic Safety Administration's public vPIC "
                "service to be decoded. Nothing else about you is sent with it, and "
                "we do not keep the numbers you look up.",
            )
        )
    return out


def _legal_urls(job_id: str, storage: S3ArtifactStorage) -> dict[str, str]:
    """The published privacy/terms/support URLs for a job, empty when not yet published."""
    from iosforge.mvp.store_listing import APPLE_EULA_URL

    key = build_key(job_id=job_id, kind="legal", name="urls.json")
    if not storage.exists(key):
        return {"privacy_policy_url": "", "terms_url": APPLE_EULA_URL, "support_url": ""}
    data = json.loads(storage.get(key))
    return {
        "privacy_policy_url": str(data.get("privacy_policy_url") or ""),
        "terms_url": str(data.get("terms_url") or APPLE_EULA_URL),
        "support_url": str(data.get("support_url") or ""),
    }


@celery_app.task(base=PipelineTask, name="iosforge.generate_store_listing", bind=True)
def generate_store_listing(
    self,
    job_id: str,
    subtitle: str = "",
    keyword_seed: list[str] | None = None,
    lead: str = "",
) -> str:
    """Draft the App Store listing (description, keywords, subtitle) from the app's spec.

    Independent of the build: it needs the spec and the published legal URLs only, so the
    listing can be re-rolled without touching the app. Stored as ``store_listing/listing.json``
    for the admin to review and push. Not copied from the original app — generated fresh.
    """
    from iosforge.mvp import store_listing

    storage = S3ArtifactStorage()
    maker = get_sessionmaker()
    with maker() as db:
        job = db.get(Job, uuid.UUID(job_id))
        if job is None:
            return f"job {job_id} not found"
        metadata = dict(job.source_app_metadata or {})

    spec = json.loads(storage.get(build_key(job_id=job_id, kind="app_spec", name="app_spec.json")))
    legal = _legal_urls(job_id, storage)
    app_name = str(metadata.get("app_name") or spec.get("app_name") or spec.get("name") or "")
    has_sub = bool(
        spec.get("monetization")
        or spec.get("subscriptions")
        or "paywall" in json.dumps(spec).lower()
    )

    listing = store_listing.generate(
        spec,
        app_name=app_name,
        privacy_policy_url=legal["privacy_policy_url"],
        terms_url=legal["terms_url"],
        subtitle=subtitle,
        keyword_seed=keyword_seed or [],
        lead=lead,
        has_subscription=has_sub,
    )
    storage.put(
        build_key(job_id=job_id, kind="store_listing", name="listing.json"),
        json.dumps(listing.to_dict(), indent=2, ensure_ascii=False).encode("utf-8"),
        content_type="application/json",
    )
    log.info(
        "store_listing.generated",
        job_id=job_id,
        keywords=listing.keywords[:60],
        warnings=len(listing.warnings),
    )
    return f"job {job_id} store listing drafted ({len(listing.description)} chars)"


@celery_app.task(base=PipelineTask, name="iosforge.push_store_listing", bind=True)
def push_store_listing(self, job_id: str) -> str:
    """Write the drafted listing to App Store Connect via the job's own API key.

    Targets the app's editable version + app-info localisation, so nothing is written to a
    version already in review or on sale. Description, keywords, promotional text and
    support/marketing URLs go on the version; subtitle and privacy policy URL on the app
    info. Requires the job's ASC signing key and a generated listing.
    """
    from iosforge.mvp import asc_api, asc_credentials, store_listing

    settings = get_settings()
    storage = S3ArtifactStorage()
    maker = get_sessionmaker()
    with maker() as db:
        job = db.get(Job, uuid.UUID(job_id))
        if job is None:
            return f"job {job_id} not found"
        metadata = dict(job.source_app_metadata or {})

    listing_key = build_key(job_id=job_id, kind="store_listing", name="listing.json")
    if not storage.exists(listing_key):
        raise RuntimeError("no store listing drafted — generate it first")
    listing = store_listing.from_dict(json.loads(storage.get(listing_key)))

    creds = asc_credentials.load(settings, job_id)
    if creds is None:
        raise RuntimeError("no App Store Connect API key uploaded for this app")

    bundle_id = str(metadata.get("bundle_id") or "")
    with asc_api.AscClient(creds) as client:
        target = client.editable_listing(bundle_id=bundle_id)
        written: list[str] = []
        if target.version_localization_id:
            version_attrs = {
                "description": listing.description,
                "keywords": listing.keywords,
                "promotionalText": listing.promotional_text,
            }
            if listing.support_url:
                version_attrs["supportUrl"] = listing.support_url
            if listing.marketing_url:
                version_attrs["marketingUrl"] = listing.marketing_url
            client.write_version_localization(target.version_localization_id, version_attrs)
            written.append("description/keywords")
        if target.info_localization_id:
            info_attrs: dict[str, str] = {"subtitle": listing.subtitle}
            if listing.privacy_policy_url:
                info_attrs["privacyPolicyUrl"] = listing.privacy_policy_url
            # The store name is writable only while the app info is editable — Apple
            # locks it once a version is in review. Attempt it, but never let a locked
            # name lose the rest of the listing.
            if listing.app_name:
                try:
                    client.write_info_localization(
                        target.info_localization_id, {**info_attrs, "name": listing.app_name}
                    )
                    written.append("name/subtitle/privacy")
                except asc_api.AscApiError as exc:
                    log.warning("store_listing.name_locked", job_id=job_id, error=str(exc))
                    client.write_info_localization(target.info_localization_id, info_attrs)
                    written.append("subtitle/privacy (name locked)")
            else:
                client.write_info_localization(target.info_localization_id, info_attrs)
                written.append("subtitle/privacy")

    log.info(
        "store_listing.pushed",
        job_id=job_id,
        app_id=target.app_id,
        state=target.version_state,
        wrote=written,
    )
    return f"job {job_id} listing pushed to App Store Connect ({', '.join(written) or 'nothing'})"


@celery_app.task(base=PipelineTask, name="iosforge.autofill_store_metadata", bind=True)
def autofill_store_metadata(
    self,
    job_id: str,
    primary_category: str = "UTILITIES",
    secondary_category: str = "",
    copyright_holder: str = "",
) -> str:
    """Fill every App Store Connect field the API can set without human judgement.

    Category, content-rights declaration, the age-rating questionnaire (answered as an app
    with no objectionable content → 4+), the Support URL and the copyright line — the fields
    that otherwise have to be clicked through in the web UI before a version can be
    submitted. Submission is blocked outright without the copyright, so it is filled from the
    account holder's organisation unless the caller names one. The privacy policy and store
    copy come from their own steps; this covers what is left. The build is NOT attached
    here — that is a deliberate, version-specific choice left to the operator.
    """
    from iosforge.mvp import asc_api, asc_credentials

    settings = get_settings()
    storage = S3ArtifactStorage()
    maker = get_sessionmaker()
    with maker() as db:
        job = db.get(Job, uuid.UUID(job_id))
        if job is None:
            return f"job {job_id} not found"
        metadata = dict(job.source_app_metadata or {})

    creds = asc_credentials.load(settings, job_id)
    if creds is None:
        raise RuntimeError("no App Store Connect API key uploaded for this app")

    legal = _legal_urls(job_id, storage)
    support_url = str(legal.get("support_url") or "")
    bundle_id = str(metadata.get("bundle_id") or "")
    done: list[str] = []

    with asc_api.AscClient(creds) as client:
        app = client.find_app(bundle_id)
        app_id = str(app["id"])
        target = client.editable_listing(bundle_id=bundle_id)

        infos = client.get(f"/apps/{app_id}/appInfos", **{"include": "ageRatingDeclaration"})
        info = infos.get("data", [{}])[0]
        info_id = str(info.get("id") or "")
        age_decl = next(
            (i["id"] for i in infos.get("included", []) if i["type"] == "ageRatingDeclarations"),
            "",
        )

        if info_id:
            client.set_categories(info_id, primary=primary_category, secondary=secondary_category)
            done.append(f"category {primary_category}")
        try:
            client.set_content_rights(app_id, "DOES_NOT_USE_THIRD_PARTY_CONTENT")
            done.append("content rights")
        except asc_api.AscApiError as exc:
            log.warning("autofill.content_rights_failed", job_id=job_id, error=str(exc))
        if age_decl:
            client.set_age_rating(age_decl, asc_api.AGE_RATING_NO_OBJECTIONABLE)
            done.append("age rating")
        if support_url and target.version_localization_id:
            client.write_version_localization(
                target.version_localization_id, {"supportUrl": support_url}
            )
            done.append("support URL")

        holder = copyright_holder.strip() or _copyright_line(client)
        if holder:
            client.set_copyright(target.version_id, holder)
            done.append("copyright")

    log.info("autofill.done", job_id=job_id, app_id=app_id, filled=done)
    return f"job {job_id} App Store Connect fields filled: {', '.join(done) or 'nothing'}"


def _copyright_line(client: object) -> str:
    """``<year> <organisation>`` for the copyright field, from the account holder.

    Apple wants the rights holder, not the app name, and blocks submission without it.
    The account holder's own name is the honest fallback when the API exposes no
    organisation.
    """
    import datetime as _dt

    holder = client.account_holder()  # type: ignore[attr-defined]
    name = " ".join(x for x in (holder.get("first_name"), holder.get("last_name")) if x)
    return f"{_dt.datetime.now(_dt.UTC).year} {name}".strip() if name else ""


@celery_app.task(base=PipelineTask, name="iosforge.set_review_contact", bind=True)
def set_review_contact(
    self,
    job_id: str,
    first_name: str,
    last_name: str,
    phone: str,
    email: str,
    notes: str = "",
) -> str:
    """Write the App Review contact (name, phone, email) onto the editable version.

    Apple requires this before a version can be submitted and does not expose it as a
    build detail, so it lives here as its own step the admin can trigger.
    """
    from iosforge.mvp import asc_api, asc_credentials

    settings = get_settings()
    maker = get_sessionmaker()
    with maker() as db:
        job = db.get(Job, uuid.UUID(job_id))
        if job is None:
            return f"job {job_id} not found"
        bundle_id = str((job.source_app_metadata or {}).get("bundle_id") or "")

    creds = asc_credentials.load(settings, job_id)
    if creds is None:
        raise RuntimeError("no App Store Connect API key uploaded for this app")

    attributes: dict[str, object] = {
        "contactFirstName": first_name.strip(),
        "contactLastName": last_name.strip(),
        "contactPhone": phone.strip(),
        "contactEmail": email.strip(),
        "demoAccountRequired": False,
    }
    if notes.strip():
        attributes["notes"] = notes.strip()

    with asc_api.AscClient(creds) as client:
        target = client.editable_listing(bundle_id=bundle_id)
        client.set_review_contact(target.version_id, attributes)

    log.info("review_contact.set", job_id=job_id, email=email)
    return f"job {job_id} review contact set: {first_name} {last_name}"


@celery_app.task(base=PipelineTask, name="iosforge.generate_app_icon", bind=True)
def generate_app_icon(self, job_id: str) -> str:
    """Draw the clone's own app icon from the source app's icon.

    Independent of the build: it needs the spec and the source listing's artwork,
    nothing else, so the icon can be re-rolled until it satisfies without touching
    the app. Stored as ``app_icon/app_icon.png`` — 1024x1024, opaque, unrounded,
    which is what App Store Connect accepts.
    """
    from iosforge.mvp import app_icon, store_assets

    settings = get_settings()
    storage = S3ArtifactStorage()
    maker = get_sessionmaker()
    tmp = Path(tempfile.mkdtemp(prefix="iosforge-icon-"))
    try:
        with maker() as db:
            job = db.get(Job, uuid.UUID(job_id))
            if job is None:
                return f"job {job_id} not found"
            metadata = dict(job.source_app_metadata or {})

        spec = json.loads(
            storage.get(build_key(job_id=job_id, kind="app_spec", name="app_spec.json"))
        )
        # The icon must match the app that actually ships. Codegen applies its own
        # anti-clone divergence, so the palette it compiled can disagree with the
        # spec's tokens entirely — read the app's colours first and fall back.
        palette = _app_palette(job_id, storage) or store_assets.palette(
            spec.get("design_tokens") or {}
        )
        icon = app_icon.generate(spec, metadata, tmp, settings=settings, palette=palette)
        key = build_key(job_id=job_id, kind="app_icon", name=app_icon.ICON_NAME)
        storage.put(key, icon.read_bytes(), content_type="image/png")
        log.info("app_icon.stored", job_id=job_id, key=key)
        return f"job {job_id} app icon: {icon.stat().st_size // 1024} KB"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@celery_app.task(base=PipelineTask, name="iosforge.generate_store_assets", bind=True)
def generate_store_assets(self, job_id: str) -> str:
    """Render the App Store listing screenshots for an already-built app.

    Reference material is the SOURCE app's listing (composition only); the content is
    ours — our real rendered screens, our divergent palette and our own copy. The
    planner picks, per source slide, which of our screens actually shows the promised
    feature, so the listing never advertises something the app does not do.

    Independent of the build: it needs the generated screens and the spec, nothing else,
    so it can be re-run to iterate on the listing without touching the app.
    """
    from iosforge.mvp import image_slides, slide_contract, store_assets

    settings = get_settings()
    storage = S3ArtifactStorage()
    maker = get_sessionmaker()
    tmp = Path(tempfile.mkdtemp(prefix="iosforge-store-"))
    try:
        with maker() as db:
            job = db.get(Job, uuid.UUID(job_id))
            if job is None:
                return f"job {job_id} not found"
            app_id = str((job.source_app_metadata or {}).get("appstore_app_id") or "")
            if not app_id and job.source_app_ref:
                found = re.search(r"id(\d+)", job.source_app_ref)
                app_id = found.group(1) if found else ""
            country = str((job.source_app_metadata or {}).get("appstore_country") or "us")

        work = tmp / "ws"
        work.mkdir(parents=True, exist_ok=True)
        screens_dir = work / "screens"
        screens_dir.mkdir(exist_ok=True)

        spec_raw = storage.get(build_key(job_id=job_id, kind="app_spec", name="app_spec.json"))
        spec = json.loads(spec_raw)
        (work / "app_spec.json").write_bytes(spec_raw)

        # Our own rendered screens, keyed by the spec's screen ids.
        rendered: list[str] = []
        for entry in spec.get("screens", []) or []:
            sid = str(entry.get("id") or "")
            if not sid:
                continue
            try:
                data = storage.get(
                    build_key(job_id=job_id, kind="generated_screenshots", name=f"{sid}.png")
                )
            except Exception:
                continue
            (screens_dir / f"{sid}.png").write_bytes(data)
            rendered.append(sid)
        if not rendered:
            return f"job {job_id} has no generated screenshots to build a listing from"

        (work / "screens.json").write_text(
            json.dumps(
                [
                    {
                        "id": str(e.get("id")),
                        "name": e.get("name"),
                        "purpose": e.get("purpose"),
                    }
                    for e in spec.get("screens", []) or []
                    if str(e.get("id") or "") in set(rendered)
                ],
                indent=2,
                ensure_ascii=False,
            )
        )
        originals_dir = work / "original"
        originals = store_assets.download_original_slides(app_id, originals_dir, country=country)
        log.info(
            "store_assets.staged", job_id=job_id, screens=len(rendered), originals=len(originals)
        )

        # A previous run's output is the starting point: this task is acks_late and
        # costs hours, so a restart that re-analysed and re-authored everything never
        # reached the refine loop. Whatever is already on record is replayed first.
        def _stored(name: str) -> bytes | None:
            key = build_key(job_id=job_id, kind="store_assets", name=name)
            return storage.get(key) if storage.exists(key) else None

        # Stage 1 — analyse EVERY source slide on its own into its own contract.
        contracts_dir = work / "contracts"
        previous = _stored("slide_contracts.json")
        if previous:
            slide_contract.restore_contracts(contracts_dir, json.loads(previous))
        # Gate: no contract for a slide means the listing would silently shrink. A
        # restored set that already satisfies it makes the analysis pass unnecessary.
        try:
            contracts = slide_contract.verify_analysis(originals_dir, contracts_dir)
        except slide_contract.IncompleteAnalysisError:
            slide_contract.analyse(work, timeout=settings.store_assets_timeout_s)
            contracts = slide_contract.verify_analysis(originals_dir, contracts_dir)

        tokens = spec.get("design_tokens") or {}
        out_dir = tmp / "out"

        # Stage 2 — draw each contract. The image engine returns a finished slide per
        # source in about a minute; the HTML engine has an agent author a page that
        # Chromium screenshots, which costs minutes but keeps text and colour exact.
        # A configured engine with no token silently falls back rather than failing a
        # job for a missing secret.
        if settings.store_assets_engine == "replicate" and image_slides.is_configured(settings):
            token = image_slides.load_token(settings)
            palette = store_assets.palette(tokens)
            pngs = []
            for contract in contracts:
                index = int(contract.get("index") or 0)
                source = originals_dir / f"{index:02d}.png"
                if not source.is_file():
                    continue
                try:
                    pngs.append(
                        image_slides.render_slide(
                            contract,
                            source,
                            out_dir / f"{index:02d}.png",
                            settings=settings,
                            token=token,
                            palette=palette,
                        )
                    )
                except Exception as exc:
                    log.warning(
                        "store_assets.slide_failed", job_id=job_id, index=index, error=str(exc)
                    )
            if not pngs:
                return f"job {job_id} produced no slides"
            return _store_slides(storage, job_id, pngs, pages=[], contracts=contracts, review={})

        store_assets.stage_assets(work)
        store_assets.prefetch_backgrounds(work, contracts)
        store_assets.prefetch_props(work, contracts)
        threshold = settings.store_assets_similarity_min
        prior = _stored("review.json")
        store_assets.restore_pages(
            work,
            store_assets.resumable_slides(
                json.loads(prior) if prior else {},
                [int(c.get("index", 0)) for c in contracts if c.get("index")],
                threshold,
            ),
            _stored,
        )
        pages = store_assets.build_slide_pages(
            work, tokens, timeout=settings.store_assets_timeout_s
        )
        if not pages:
            return f"job {job_id} produced no slide pages"

        pngs = store_assets.render_pages(work, pages, out_dir)

        # Stage 3 — score each render against the source composition and re-author the
        # ones that fail. Authoring is blind: without this loop nobody ever looks at the
        # result, which is how doubled UI and decoration across text survived before.
        review: dict[str, Any] = {}
        # The review/refine agents are long-running too; a timeout here must NOT bubble
        # up and retry the whole (already expensive) build. Best-effort polish only.
        try:
            for attempt in range(1, settings.store_assets_max_iterations + 1):
                review = store_assets.review_slides(
                    work, originals_dir, pngs, timeout=settings.store_assets_timeout_s
                )
                failing = store_assets.failing_slides(review, threshold)
                scores = store_assets.review_scores(review)
                log.info(
                    "store_assets.reviewed",
                    job_id=job_id,
                    attempt=attempt,
                    failing=len(failing),
                    lowest=min(scores.values()) if scores else None,
                    mean=round(sum(scores.values()) / len(scores)) if scores else None,
                )
                if not failing:
                    break
                if attempt == settings.store_assets_max_iterations:
                    log.warning(
                        "store_assets.below_threshold",
                        job_id=job_id,
                        slides=failing,
                        threshold=threshold,
                    )
                    break
                store_assets.refine_slides(
                    work, threshold=threshold, timeout=settings.store_assets_timeout_s
                )
                pngs = store_assets.render_pages(work, pages, out_dir)
        except Exception as exc:
            log.warning("store_assets.review_skipped", job_id=job_id, error=str(exc))

        return _store_slides(storage, job_id, pngs, pages=pages, contracts=contracts, review=review)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _store_slides(
    storage,
    job_id: str,
    pngs: list[Path],
    *,
    pages: list[Path],
    contracts: list[dict[str, Any]],
    review: dict[str, Any],
) -> str:
    """Persist a finished listing and report what was produced against what was planned.

    Shared by both engines. A short listing is reported WITHOUT raising: an exception
    here would send the whole expensive task into a Celery retry, so what succeeded is
    stored and the gap is surfaced in the result and a warning.
    """
    from iosforge.mvp import slide_contract, store_assets

    dropped = slide_contract.missing_slides(contracts, pngs)
    if dropped:
        log.warning(
            "store_assets.incomplete",
            job_id=job_id,
            made=len(pngs),
            expected=len(contracts),
            missing=dropped,
        )

    keys: list[str] = []
    for png in pngs:
        key = build_key(job_id=job_id, kind="store_assets", name=png.name)
        storage.put(key, png.read_bytes(), content_type="image/png")
        keys.append(key)
    # Authored pages are the editable source of a slide; the image engine has none.
    for page in pages:
        storage.put(
            build_key(job_id=job_id, kind="store_assets", name=page.name),
            page.read_bytes(),
            content_type="text/html",
        )
    # The contracts are what generation consumed — keep them alongside the output so
    # a disputed slide can be traced back to the analysis it came from.
    storage.put(
        build_key(job_id=job_id, kind="store_assets", name="slide_contracts.json"),
        json.dumps(contracts, indent=2, ensure_ascii=False).encode(),
        content_type="application/json",
    )
    storage.put(
        build_key(job_id=job_id, kind="store_assets", name="review.json"),
        json.dumps(review, indent=2, ensure_ascii=False).encode(),
        content_type="application/json",
    )
    scores = store_assets.review_scores(review)
    mean_score = round(sum(scores.values()) / len(scores)) if scores else None
    log.info(
        "store_assets.done",
        job_id=job_id,
        slides=len(keys),
        contracts=len(contracts),
        mean_score=mean_score,
    )
    gap = f", missing {', '.join(dropped)}" if dropped else ""
    scored = f", similarity {mean_score}%" if mean_score is not None else ""
    return f"job {job_id} store assets: {len(keys)}/{len(contracts)} slide(s){scored}{gap}"


def _rehydrate_store_workspace(job_id: str, storage, spec: dict[str, Any], work: Path) -> None:
    """Rebuild the store-assets workspace from stored artifacts for a per-slide edit.

    A slide's HTML references its screen and backdrop by relative path, so the pages,
    the app screens, the fetched backdrops and the contracts are all restored where the
    edit + re-render expect them.
    """
    from iosforge.mvp import store_assets

    (work / "slides").mkdir(parents=True, exist_ok=True)
    (work / "screens").mkdir(parents=True, exist_ok=True)

    for entry in spec.get("screens", []) or []:
        sid = str(entry.get("id") or "")
        if not sid:
            continue
        try:
            data = storage.get(
                build_key(job_id=job_id, kind="generated_screenshots", name=f"{sid}.png")
            )
        except Exception:
            continue
        (work / "screens" / f"{sid}.png").write_bytes(data)

    # The edit references render/NN.png (how it looks now); stage it from the stored PNG.
    (work / "render").mkdir(parents=True, exist_ok=True)
    for idx in range(1, 11):
        try:
            html = storage.get(
                build_key(job_id=job_id, kind="store_assets", name=f"{idx:02d}.html")
            )
        except Exception:
            continue
        (work / "slides" / f"{idx:02d}.html").write_bytes(html)
        try:
            png = storage.get(build_key(job_id=job_id, kind="store_assets", name=f"{idx:02d}.png"))
            (work / "render" / f"{idx:02d}.png").write_bytes(png)
        except Exception:
            pass

    try:
        contracts = json.loads(
            storage.get(build_key(job_id=job_id, kind="store_assets", name="slide_contracts.json"))
        )
        (work / "slide_contracts.json").write_text(json.dumps(contracts, ensure_ascii=False))
        store_assets.stage_assets(work)
        store_assets.prefetch_backgrounds(work, contracts)
        store_assets.prefetch_props(work, contracts)
    except Exception:
        pass

    (work / "tokens.json").write_text(
        json.dumps(spec.get("design_tokens") or {}, ensure_ascii=False)
    )


@celery_app.task(base=PipelineTask, name="iosforge.generate_ipad_slides", bind=True)
def generate_ipad_slides(
    self,
    job_id: str,
    count: int = 3,
    swap_device: bool = True,
    variant: str = "",
    notes: str = "",
    indices: list[int] | None = None,
) -> str:
    """Re-frame the first slides of the listing for the iPad canvas.

    Built FROM our finished phone slides rather than from the competitor's artwork:
    the wording, palette and subject are already settled, and re-deriving them would
    only reopen decisions that were made deliberately. Only the shape changes —
    0.46 wide-to-tall becomes 0.75 — and, where a phone is pictured, the device
    itself becomes a tablet.

    Only the leading slides are made: those are the ones a listing is judged on, and
    an iPad set does not have to match the iPhone set slide for slide.

    ``variant`` writes beside the set instead of over it, so several takes can be
    compared before one is adopted. ``notes`` steer a take without touching the
    standing rules. ``indices`` re-frames named slides instead of the leading ones,
    for when the tablet set is not simply the first few.
    """
    from iosforge.mvp import image_slides

    settings = get_settings()
    storage = S3ArtifactStorage()
    tmp = Path(tempfile.mkdtemp(prefix="iosforge-ipad-"))
    try:
        token = image_slides.load_token(settings)
        if not token:
            return f"job {job_id}: no Replicate token configured"

        made: list[int] = []
        wanted = indices if indices else list(range(1, 11))
        for index in wanted:
            if not indices and len(made) >= count:
                break
            source_key = build_key(job_id=job_id, kind="store_assets", name=f"{index:02d}.png")
            if not storage.exists(source_key):
                continue
            phone = tmp / f"{index:02d}.png"
            phone.write_bytes(storage.get(source_key))
            try:
                prompt = image_slides.ipad_prompt(swap_device=swap_device)
                if notes.strip():
                    prompt += (
                        "\n\nOPERATOR CORRECTIONS — these override anything above "
                        f"that contradicts them:\n{notes.strip()}"
                    )
                data = image_slides.generate_image(
                    prompt,
                    [image_slides.upload_image(phone, token=token)],
                    token=token,
                    model=settings.replicate_model,
                    resolution=settings.replicate_resolution,
                    timeout=settings.replicate_timeout_s,
                    size=image_slides.IPAD_CANVAS,
                )
            except Exception as exc:
                log.warning("ipad_slide.failed", job_id=job_id, index=index, error=str(exc))
                continue
            name = f"ipad_{index:02d}.png"
            if variant:
                safe = re.sub(r"[^a-z0-9-]+", "-", variant.lower()).strip("-") or "v"
                name = f"ipad_{index:02d}_{safe}.png"
            storage.put(
                build_key(job_id=job_id, kind="store_assets", name=name),
                image_slides.to_png(data),
                content_type="image/png",
            )
            made.append(index)
            log.info("ipad_slide.done", job_id=job_id, index=index, bytes=len(data))

        if not made:
            return f"job {job_id} produced no iPad slides"
        return f"job {job_id} iPad slides: {', '.join(f'{i:02d}' for i in made)}"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@celery_app.task(base=PipelineTask, name="iosforge.generate_store_slide", bind=True)
def generate_store_slide(
    self,
    job_id: str,
    index: int,
    notes: str = "",
    from_current: bool = False,
    variant: str = "",
    text_free: bool = False,
) -> str:
    """Draw ONE listing slide with the image engine, leaving the others alone.

    The unit an operator actually works in: look at a slide, say what is wrong, get
    that slide back. ``notes`` are appended to the prompt as corrections that
    override the standing rules, so a re-run answers the feedback instead of
    rolling the dice again. The contract is untouched — it records what the source
    slide contained, which does not change because our render was off.

    ``from_current`` edits OUR slide instead of redrawing from the competitor's.
    Once a slide is close, starting over throws away everything that already works
    and re-rolls the parts that were fine; editing keeps them.

    ``variant`` writes the result beside the slide instead of over it, so several
    attempts can be compared before one replaces the listing. Variants are named
    ``NN_<variant>.png`` and never appear in the listing itself.
    """
    from iosforge.mvp import image_slides, store_assets

    settings = get_settings()
    storage = S3ArtifactStorage()
    maker = get_sessionmaker()
    tmp = Path(tempfile.mkdtemp(prefix="iosforge-slide-"))
    try:
        with maker() as db:
            job = db.get(Job, uuid.UUID(job_id))
            if job is None:
                return f"job {job_id} not found"
            meta = job.source_app_metadata or {}
            app_id = str(meta.get("appstore_app_id") or "")
            if not app_id and job.source_app_ref:
                found = re.search(r"id(\d+)", job.source_app_ref)
                app_id = found.group(1) if found else ""
            country = str(meta.get("appstore_country") or "us")

        token = image_slides.load_token(settings)
        if not token:
            return f"job {job_id} slide {index}: no Replicate token configured"

        spec = json.loads(
            storage.get(build_key(job_id=job_id, kind="app_spec", name="app_spec.json"))
        )
        contract = _slide_contract(storage, job_id, index, spec)

        slide_key = build_key(job_id=job_id, kind="store_assets", name=f"{index:02d}.png")
        if from_current:
            # Editing our own render: it IS the composition, so the competitor's
            # slide is not needed and would only pull the result back towards it.
            if not storage.exists(slide_key):
                return f"job {job_id} has no slide {index:02d} to edit"
            image_slides_source = tmp / f"current_{index:02d}.png"
            image_slides_source.write_bytes(storage.get(slide_key))
        else:
            originals = tmp / "original"
            image_slides_source = originals / f"{index:02d}.png"
            store_assets.download_original_slides(app_id, originals, country=country)
            if not image_slides_source.is_file():
                return f"job {job_id} could not fetch source slide {index:02d}"

        palette = _app_palette(job_id, storage) or store_assets.palette(
            spec.get("design_tokens") or {}
        )
        # Our own icon rides along as a second reference so a slide that shows the
        # app's mark draws OURS, not the source's — the model otherwise copies the
        # icon it can see in the screenshot, colours and all.
        icon_path: Path | None = None
        icon_key = build_key(job_id=job_id, kind="app_icon", name=image_slides.ICON_REF_NAME)
        try:
            if storage.exists(icon_key):
                icon_path = tmp / "app_icon.png"
                icon_path.write_bytes(storage.get(icon_key))
        except Exception as exc:
            log.warning("store_slide.icon_unavailable", job_id=job_id, error=str(exc))
            icon_path = None

        out = image_slides.render_slide(
            contract,
            image_slides_source,
            tmp / "out" / f"{index:02d}.png",
            settings=settings,
            token=token,
            palette=palette,
            notes=notes,
            icon=icon_path,
            editing=from_current,
            text_free=text_free,
        )
        target = slide_key
        if variant:
            safe = re.sub(r"[^a-z0-9-]+", "-", variant.lower()).strip("-") or "v"
            target = build_key(job_id=job_id, kind="store_assets", name=f"{index:02d}_{safe}.png")
        storage.put(target, out.read_bytes(), content_type="image/png")
        log.info("store_slide.done", job_id=job_id, index=index, notes=bool(notes.strip()))
        return f"job {job_id} slide {index:02d}: {out.stat().st_size // 1024} KB"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@celery_app.task(base=PipelineTask, name="iosforge.regenerate_store_slide", bind=True)
def regenerate_store_slide(self, job_id: str, index: int, instructions: str) -> str:
    """Refine ONE listing slide in place from the HTML that already exists.

    Rehydrates only what that slide needs, edits its page per the operator's request
    (never from scratch), re-renders just that slide and stores it back. The other
    slides are untouched.
    """
    from iosforge.mvp import store_assets

    settings = get_settings()
    storage = S3ArtifactStorage()
    maker = get_sessionmaker()
    tmp = Path(tempfile.mkdtemp(prefix="iosforge-slide-"))
    try:
        with maker() as db:
            job = db.get(Job, uuid.UUID(job_id))
            if job is None:
                return f"job {job_id} not found"
            app_id = str((job.source_app_metadata or {}).get("appstore_app_id") or "")
            if not app_id and job.source_app_ref:
                found = re.search(r"id(\d+)", job.source_app_ref)
                app_id = found.group(1) if found else ""
            country = str((job.source_app_metadata or {}).get("appstore_country") or "us")
        spec = json.loads(
            storage.get(build_key(job_id=job_id, kind="app_spec", name="app_spec.json"))
        )

        work = tmp / "ws"
        work.mkdir(parents=True, exist_ok=True)
        _rehydrate_store_workspace(job_id, storage, spec, work)
        # The source slide is the edit's fidelity reference.
        store_assets.download_original_slides(app_id, work / "original", country=country)

        page = store_assets.edit_slide(
            work, index, instructions, timeout=settings.store_assets_timeout_s
        )
        if page is None:
            return f"job {job_id} slide {index:02d} not found to edit"

        rendered = store_assets.render_pages(work, [page], tmp / "out")
        if not rendered:
            return f"job {job_id} slide {index:02d} failed to render after edit"

        storage.put(
            build_key(job_id=job_id, kind="store_assets", name=f"{index:02d}.html"),
            page.read_bytes(),
            content_type="text/html",
        )
        storage.put(
            build_key(job_id=job_id, kind="store_assets", name=f"{index:02d}.png"),
            rendered[0].read_bytes(),
            content_type="image/png",
        )
        log.info("store_assets.slide_regenerated", job_id=job_id, index=index)
        return f"job {job_id} slide {index:02d} regenerated"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


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
