"""Celery tasks of the native SwiftUI path (Mac worker): delivery, and shared helpers.

The SwiftUI app travels between tasks as ``jobs/<id>/sources/xcode_app.zip``. Every task
here needs the Apple toolchain; on a host without it the task fails loudly
(``SimulatorUnavailable``) instead of pretending to succeed — routing these tasks to a
Mac worker is a deployment decision (Mac ↔ Linux connectivity), not code.
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
from iosforge.db.models import Job, StageTimeline, XcodeBuild
from iosforge.db.session import get_sessionmaker
from iosforge.mvp import (
    apphud_provision,
    attribution,
    build_profile,
    ios_delivery,
    swiftui_integrations,
)
from iosforge.mvp.paths import RunPaths
from iosforge.mvp.swiftui_scaffold import write_scaffold
from iosforge.storage.client import ArtifactStorage, S3ArtifactStorage, build_key

log = get_logger("worker.swiftui")

SOURCES_NAME = "xcode_app.zip"
LOG_LIMIT = 200_000


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


def _icon(storage: ArtifactStorage, job_id: str, out: Path) -> Path | None:
    key = build_key(job_id=job_id, kind="app_icon", name="app_icon.png")
    if not storage.exists(key):
        return None
    out.write_bytes(storage.get(key))
    return out


@celery_app.task(base=PipelineTask, name="iosforge.run_xcode_delivery", bind=True)
def run_xcode_delivery(self: Any, job_id: str) -> str:
    """Archive the job's SwiftUI app and export an IPA (upload stays gated).

    Refreshes the contract with the job's current identity, integrations and icon,
    then runs :func:`iosforge.mvp.ios_delivery.deliver`. Records an ``XcodeBuild`` row
    and a DELIVERY timeline stage, and leaves the job DONE (or FAILED with the error).
    """
    settings = get_settings()
    storage = S3ArtifactStorage()
    maker = get_sessionmaker()
    with maker() as db:
        job = db.get(Job, uuid.UUID(job_id))
        if job is None:
            return f"job {job_id} not found"
        build = XcodeBuild(job_id=job.id, status="archiving", started_at=utcnow())
        db.add(build)
        job.state = JobState.DELIVERY
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
            job.state = JobState.FAILED if result.status == "failed" else JobState.DONE
            if result.status == "failed":
                stage.error = build.message
        except Exception as exc:
            log.error("swiftui.delivery.failed", job_id=job_id, error=str(exc))
            build.status = "failed"
            build.message = str(exc)[:4000]
            stage.error = build.message
            job.state = JobState.FAILED
        build.finished_at = utcnow()
        if build.started_at:
            build.duration_ms = int((build.finished_at - build.started_at).total_seconds() * 1000)
        stage.finished_at = build.finished_at
        stage.duration_ms = build.duration_ms
        db.commit()
        return f"job {job_id} delivery {build.status}"


def latest_xcode_build(db: Session, job_id: uuid.UUID) -> XcodeBuild | None:
    """The job's most recent native delivery (for the admin)."""
    return db.scalar(
        select(XcodeBuild).where(XcodeBuild.job_id == job_id).order_by(XcodeBuild.created_at.desc())
    )
