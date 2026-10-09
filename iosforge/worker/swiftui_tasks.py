"""Celery tasks of the native SwiftUI path (Mac worker): delivery, and shared helpers.

The SwiftUI app travels between tasks as ``jobs/<id>/sources/xcode_app.zip``. Every task
here needs the Apple toolchain; on a host without it the task fails loudly
(``SimulatorUnavailable``) instead of pretending to succeed. The tasks go to their own
``xcode`` queue, outside the stage queues Linux workers serve, so only a worker started
with ``-Q xcode`` on a Mac picks them up; wiring that worker to the broker is a
deployment decision (Mac ↔ Linux connectivity), not code.

Delivery never changes ``Job.state``: like the CodeMagic build it is an operator action
on a finished job, tracked only by its ``XcodeBuild`` row and the DELIVERY timeline.
"""

from __future__ import annotations

import io
import json
import shutil
import tempfile
import uuid
import zipfile
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from iosforge.common.config import Settings, get_settings
from iosforge.common.logging import get_logger
from iosforge.common.queue import PipelineTask, celery_app
from iosforge.common.types import JobState, Stage
from iosforge.db.base import utcnow
from iosforge.db.models import (
    DataArchiveArtifact,
    GenerationResult,
    Job,
    StageTimeline,
    WalkthroughResult,
    XcodeBuild,
)
from iosforge.db.session import get_sessionmaker
from iosforge.mvp import (
    apphud_provision,
    attribution,
    build_profile,
    compliance,
    feasibility,
    frida_ingest,
    ios_delivery,
    simulator,
    swiftui_gen,
    swiftui_integrations,
)
from iosforge.mvp.paths import RunPaths
from iosforge.mvp.source_locale import resolve_source_locale
from iosforge.mvp.swiftui_scaffold import write_scaffold
from iosforge.storage.client import ArtifactStorage, S3ArtifactStorage, build_key

log = get_logger("worker.swiftui")

SOURCES_NAME = "xcode_app.zip"
LOG_LIMIT = 200_000
XCODE_QUEUE = "xcode"
ACTIVE_STATUSES = ("queued", "archiving")
DELIVERABLE_STATES = (JobState.DONE, JobState.NEEDS_INPUT)


def sources_key(job_id: str) -> str:
    """Storage key of the generated SwiftUI project of a job."""
    return build_key(job_id=job_id, kind="sources", name=SOURCES_NAME)


def store_sources(storage: ArtifactStorage, job_id: str, app_dir: Path) -> str:
    """Zip ``app_dir`` (without derived/xcodeproj files) into the job's sources key."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(app_dir.rglob("*")):
            rel = path.relative_to(app_dir)
            if path.is_file() and not any(p.endswith(".xcodeproj") for p in rel.parts):
                zf.write(path, rel.as_posix())
    key = sources_key(job_id)
    storage.put(key, buffer.getvalue(), content_type="application/zip")
    return key


def hydrate_sources(storage: ArtifactStorage, job_id: str, app_dir: Path) -> None:
    """Unpack the job's stored SwiftUI project into ``app_dir`` (refusing path escapes)."""
    if app_dir.exists():
        shutil.rmtree(app_dir)
    app_dir.mkdir(parents=True)
    root = app_dir.resolve()
    with zipfile.ZipFile(io.BytesIO(storage.get(sources_key(job_id)))) as zf:
        for member in zf.infolist():
            target = (app_dir / member.filename).resolve()
            if not target.is_relative_to(root):
                raise ValueError(f"unsafe path in sources archive: {member.filename}")
        zf.extractall(app_dir)


def job_integrations(
    job: Job,
    spec: dict[str, Any],
    paths: RunPaths,
    settings: Settings,
) -> swiftui_integrations.Integrations:
    """Provision Apphud / Tenjin for the job and freeze the native integration inputs."""
    meta = job.source_app_metadata or {}
    ident = build_profile.resolve_identity(meta, spec, settings)
    apphud = apphud_provision.provision(
        spec,
        settings=settings,
        bundle_id=ident.bundle_id,
        app_name=ident.app_name,
        sandbox=ident.sandbox,
        api_key=meta.get("apphud_api_key"),
    )
    if apphud:
        paths.apphud_config_json.write_text(json.dumps(apphud, indent=2))
    tenjin = attribution.provision(settings=settings, api_key=meta.get("tenjin_api_key"))
    if tenjin:
        paths.attribution_config_json.write_text(json.dumps(tenjin, indent=2))
    exempt = meta.get("export_compliance_exempt")
    return swiftui_integrations.collect(
        spec,
        apphud_config=paths.apphud_config_json,
        attribution_config=paths.attribution_config_json,
        skadnetwork_plist=Path(settings.skadnetwork_ids_path),
        export_compliance_exempt=exempt if isinstance(exempt, bool) else None,
    )


def active_build(db: Session, job_id: uuid.UUID) -> XcodeBuild | None:
    """The job's native delivery that is still queued or archiving (if any)."""
    return db.scalar(
        select(XcodeBuild)
        .where(XcodeBuild.job_id == job_id, XcodeBuild.status.in_(ACTIVE_STATUSES))
        .order_by(XcodeBuild.created_at.desc())
    )


def queue_delivery(db: Session, job: Job) -> XcodeBuild | None:
    """Record a ``queued`` delivery and enqueue it on the Mac queue (None if one is active)."""
    if active_build(db, job.id) is not None:
        return None
    build = XcodeBuild(job_id=job.id, status="queued")
    db.add(build)
    db.commit()
    try:
        run_xcode_delivery.apply_async(args=[str(job.id), str(build.id)], queue=XCODE_QUEUE)
    except Exception as exc:
        build.status = "failed"
        build.message = f"enqueue failed: {exc}"[:4000]
        db.commit()
        raise
    return build


def _icon(storage: ArtifactStorage, job_id: str, out: Path) -> Path | None:
    key = build_key(job_id=job_id, kind="app_icon", name="app_icon.png")
    if not storage.exists(key):
        return None
    out.write_bytes(storage.get(key))
    return out


@celery_app.task(
    base=PipelineTask, name="iosforge.run_xcode_delivery", bind=True, queue=XCODE_QUEUE
)
def run_xcode_delivery(self: Any, job_id: str, build_id: str | None = None) -> str:
    """Archive the job's SwiftUI app and export an IPA (upload stays gated).

    Refreshes the contract with the job's current identity, integrations and icon,
    then runs :func:`iosforge.mvp.ios_delivery.deliver`. Fills the ``queued``
    ``XcodeBuild`` row (or a new one) and a DELIVERY timeline stage; the job's state
    is left untouched.
    """
    settings = get_settings()
    storage = S3ArtifactStorage()
    maker = get_sessionmaker()
    with maker() as db:
        job = db.get(Job, uuid.UUID(job_id))
        if job is None:
            return f"job {job_id} not found"
        build = db.get(XcodeBuild, uuid.UUID(build_id)) if build_id else None
        if build is None:
            build = XcodeBuild(job_id=job.id)
            db.add(build)
        build.status = "archiving"
        build.started_at = utcnow()
        stage = StageTimeline(job_id=job.id, stage=Stage.DELIVERY, started_at=utcnow())
        db.add(stage)
        db.commit()
        try:
            with tempfile.TemporaryDirectory(prefix="iosforge-delivery-") as tmp:
                paths = RunPaths.create(Path(tmp) / "run")
                hydrate_sources(storage, job_id, paths.xcode_app)
                spec = json.loads(
                    storage.get(build_key(job_id=job_id, kind="app_spec", name="app_spec.json"))
                )
                ident = build_profile.resolve_identity(job.source_app_metadata, spec, settings)
                write_scaffold(
                    paths.xcode_app,
                    spec,
                    app_name=ident.app_name,
                    bundle_id=ident.bundle_id,
                    integrations=job_integrations(job, spec, paths, settings),
                    icon_png=_icon(storage, job_id, paths.run_dir / "app_icon.png"),
                )
                result = ios_delivery.deliver(
                    paths.xcode_app,
                    paths.run_dir / "delivery",
                    settings=settings,
                    job_id=job_id,
                    version=str((job.source_app_metadata or {}).get("app_version") or "1.0"),
                )
                if result.ipa:
                    ipa = Path(result.ipa)
                    build.ipa_key = build_key(job_id=job_id, kind="ipa", name=ipa.name)
                    storage.put(
                        build.ipa_key, ipa.read_bytes(), content_type="application/octet-stream"
                    )
            build.status = result.status
            build.signed = result.signed
            build.version = result.version
            build.build_number = result.build_number
            build.upload_command = list(result.upload_command)
            build.log_text = result.log[-LOG_LIMIT:]
            build.message = "; ".join(result.errors)[:4000] or None
            if result.status in ("failed", "upload_failed"):
                stage.error = build.message
        except Exception as exc:
            log.error("swiftui.delivery.failed", job_id=job_id, error=str(exc))
            build.status = "failed"
            build.message = str(exc)[:4000]
            stage.error = build.message
        build.finished_at = utcnow()
        if build.started_at:
            build.duration_ms = int((build.finished_at - build.started_at).total_seconds() * 1000)
        stage.finished_at = build.finished_at
        stage.duration_ms = build.duration_ms
        db.commit()
        return f"job {job_id} delivery {build.status}"


REWORKABLE = (JobState.DONE, JobState.FAILED, JobState.NEEDS_INPUT)


def hydrate_inputs(db: Session, job: Job, storage: ArtifactStorage, paths: RunPaths) -> None:
    """Restore the job's crawl (screens, screenshots), app_spec and Frida archive context."""
    job_id = str(job.id)
    walk = db.scalar(select(WalkthroughResult).where(WalkthroughResult.job_id == job.id))
    if walk is None:
        raise RuntimeError(f"job {job_id} has no analysis")
    paths.screens_json.write_text(json.dumps(walk.screen_map, ensure_ascii=False))
    for key in walk.screenshot_keys:
        (paths.screens_dir / key.rsplit("/", 1)[-1]).write_bytes(storage.get(key))
    paths.app_spec_json.write_bytes(
        storage.get(build_key(job_id=job_id, kind="app_spec", name="app_spec.json"))
    )
    archive = db.scalar(select(DataArchiveArtifact).where(DataArchiveArtifact.job_id == job.id))
    if archive is not None:
        raw = paths.run_dir / "data_archive.zip"
        raw.write_bytes(storage.get(archive.storage_key))
        frida_ingest.ingest_archive(raw, paths)


def _scope_ids(storage: ArtifactStorage, job_id: str, spec: dict[str, Any]) -> list[str]:
    try:
        scope = feasibility.load_scope(storage, job_id)
    except Exception:
        scope = None
    if scope is not None and scope.scope_mode == "core":
        return [s.screen_id for s in scope.screens if s.include]
    return [str(s["id"]) for s in spec.get("screens", []) if isinstance(s, dict)]


@celery_app.task(base=PipelineTask, name="iosforge.rework_swiftui", bind=True, queue=XCODE_QUEUE)
def rework_swiftui(
    self: Any, job_id: str, instructions: str = "", add_screens: list[str] | None = None
) -> str:
    """Operator rework round of a SwiftUI app (Phase 6): extend scope and/or edit, then ship.

    Allowed only while the job is not running (DONE / FAILED / NEEDS_INPUT) and its SwiftUI
    sources exist. ``add_screens`` first grows the scope from the full app_spec, then
    ``instructions`` run as one model round; a single Vision-Judge pass is recorded, the
    new version is stored (``result_version`` + 1, new ``GenerationResult``) and native
    delivery is enqueued. Fails loudly off-Mac.
    """
    settings = get_settings()
    storage = S3ArtifactStorage()
    maker = get_sessionmaker()
    with maker() as db:
        job = db.get(Job, uuid.UUID(job_id))
        if job is None:
            return f"job {job_id} not found"
        scope_pending = (job.source_app_metadata or {}).get("scope_status") == "proposed"
        if job.state not in REWORKABLE or scope_pending or not storage.exists(sources_key(job_id)):
            return f"job {job_id} cannot be reworked in state {job.state}"
        if not instructions.strip() and not add_screens:
            return f"job {job_id}: nothing to rework"
        job.state = JobState.CODEGEN
        stage = StageTimeline(job_id=job.id, stage=Stage.CODEGEN, started_at=utcnow())
        db.add(stage)
        db.commit()
        try:
            simulator.require_toolchain()
            with tempfile.TemporaryDirectory(prefix="iosforge-rework-") as tmp:
                paths = RunPaths.create(Path(tmp) / "run")
                hydrate_inputs(db, job, storage, paths)
                full_spec = json.loads(paths.app_spec_json.read_text(encoding="utf-8"))
                swiftui_gen.scope_to(paths, _scope_ids(storage, job_id, full_spec))
                hydrate_sources(storage, job_id, paths.xcode_app)
                if add_screens:
                    swiftui_gen.extend(paths, list(add_screens), full_spec)
                if instructions.strip():
                    swiftui_gen.rework(
                        paths, instructions, timeout=settings.codegen_rework_timeout_s
                    )
                report = compliance.verify_ios(
                    paths,
                    simulator.SimEnvironment(
                        udid=settings.ios_simulator_udid, locale=_job_locale(job, paths)
                    ),
                    weights=compliance.ComplianceWeights.from_settings(settings),
                    threshold=settings.frontend_verify_threshold,
                    soft_floor=settings.compliance_soft_floor,
                )
                key = store_sources(storage, job_id, paths.xcode_app)
            job.result_version = (job.result_version or 1) + 1
            db.add(
                GenerationResult(
                    job_id=job.id,
                    sources_key=key,
                    compliance_score=report.get("compliance_score"),
                    selftest_report=report,
                )
            )
            stage.finished_at = utcnow()
            job.state = JobState.DONE
            db.commit()
            try:
                queue_delivery(db, job)
            except Exception as exc:
                log.error("swiftui.rework.delivery_enqueue_failed", job_id=job_id, error=str(exc))
        except Exception as exc:
            log.error("swiftui.rework.failed", job_id=job_id, error=str(exc))
            stage.error = str(exc)[:4000]
            stage.finished_at = utcnow()
            job.state = JobState.FAILED
            db.commit()
            return f"job {job_id} rework failed: {exc}"
    return f"job {job_id} reworked (v{job.result_version})"


def _job_locale(job: Job, paths: RunPaths) -> str:
    spec = json.loads(paths.app_spec_json.read_text(encoding="utf-8"))
    manifest = (
        json.loads(paths.capture_manifest_json.read_text(encoding="utf-8"))
        if paths.capture_manifest_json.is_file()
        else None
    )
    country = (job.source_app_metadata or {}).get("country")
    return resolve_source_locale(
        spec, manifest=manifest, storefront_country=str(country) if country else None
    )


def latest_xcode_build(db: Session, job_id: uuid.UUID) -> XcodeBuild | None:
    """The job's most recent native delivery (for the admin)."""
    return db.scalar(
        select(XcodeBuild).where(XcodeBuild.job_id == job_id).order_by(XcodeBuild.created_at.desc())
    )
