"""Job routes: list, upload APK, detail, polling fragment, artifacts (SPEC §7)."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.responses import PlainTextResponse, RedirectResponse, Response, StreamingResponse
from starlette.status import HTTP_303_SEE_OTHER

from iosforge.admin import archive_validate, csrf
from iosforge.admin.appstore_url import AppStoreUrlError
from iosforge.admin.appstore_url import parse as parse_appstore_url
from iosforge.admin.deps import get_db, get_storage, require_user
from iosforge.admin.session import SessionData
from iosforge.admin.templating import templates
from iosforge.common.config import get_settings
from iosforge.common.logging import get_logger
from iosforge.common.types import JobState
from iosforge.db.models import (
    CodegenTask,
    CodemagicBuild,
    DataArchiveArtifact,
    GenerationResult,
    Job,
    StageTimeline,
    WalkthroughResult,
)
from iosforge.storage.client import ArtifactStorage, build_key

log = get_logger("admin.jobs")
router = APIRouter()


@router.get("/")
def index() -> Response:
    return RedirectResponse("/jobs", status_code=HTTP_303_SEE_OTHER)


@router.get("/jobs")
def jobs_list(
    request: Request,
    user: SessionData = Depends(require_user),
    db: Session = Depends(get_db),
) -> Response:
    jobs = db.scalars(select(Job).order_by(Job.created_at.desc()).limit(200)).all()
    return templates.TemplateResponse(request, "jobs_list.html", {"jobs": jobs, "user": user})


@router.get("/jobs/new")
def jobs_new(request: Request, user: SessionData = Depends(require_user)) -> Response:
    return templates.TemplateResponse(request, "job_new.html", {"user": user, "error": None})


@router.post("/jobs")
async def jobs_create(
    request: Request,
    csrf_token: str = Form(...),
    appstore_url: str = Form(...),
    archive: UploadFile = File(...),
    user: SessionData = Depends(require_user),
    db: Session = Depends(get_db),
    storage: ArtifactStorage = Depends(get_storage),
) -> Response:
    if not csrf.verify(user.csrf_token, csrf_token):
        return _new_error(request, user, "Invalid request (CSRF).", status=400)

    settings = get_settings()
    try:
        parsed = parse_appstore_url(appstore_url, country_default=settings.appstore_country_default)
    except AppStoreUrlError as exc:
        return _new_error(request, user, str(exc), status=400)

    max_bytes = settings.admin_upload_max_bytes
    # Stream the upload with a hard cap so an oversized file is never buffered.
    chunks: list[bytes] = []
    total = 0
    while chunk := await archive.read(1024 * 1024):
        total += len(chunk)
        if total > max_bytes:
            return _new_error(request, user, "File exceeds the size limit.", status=413)
        chunks.append(chunk)
    raw = b"".join(chunks)

    try:
        valid = archive_validate.validate(archive.filename or "archive.zip", raw, max_bytes)
    except archive_validate.ArchiveValidationError as exc:
        return _new_error(request, user, str(exc), status=400)

    job_id = uuid.uuid4()
    key = build_key(job_id=str(job_id), kind="data_archive", name=valid.filename)
    storage.put(key, valid.data, content_type="application/octet-stream")

    job = Job(
        id=job_id,
        state=JobState.QUEUED,
        source_app_ref=parsed.url,
        source_app_metadata={"app_id": parsed.app_id, "country": parsed.country},
        created_by_id=uuid.UUID(user.user_id),
        submission_kind="appstore",
    )
    db.add(job)
    db.add(
        DataArchiveArtifact(
            job_id=job_id,
            storage_key=key,
            filename=valid.filename,
            source="manual-upload",
            container=valid.container,
            sha256=valid.sha256,
            size_bytes=valid.size,
        )
    )
    db.commit()
    log.info("jobs.created", job_id=str(job_id), app_id=parsed.app_id)
    _enqueue(job_id)
    return RedirectResponse(f"/jobs/{job_id}", status_code=HTTP_303_SEE_OTHER)


@router.get("/jobs/{job_id}")
def job_detail(
    request: Request,
    job_id: uuid.UUID,
    user: SessionData = Depends(require_user),
    db: Session = Depends(get_db),
) -> Response:
    job = db.get(Job, job_id)
    if job is None:
        return Response("Not found", status_code=404)
    gen = db.scalar(select(GenerationResult).where(GenerationResult.job_id == job_id))
    can_ios_build = bool(gen and gen.codemagic and gen.codemagic.get("application_id"))
    return templates.TemplateResponse(
        request,
        "job_detail.html",
        {
            "job": job,
            "user": user,
            "timeline": _timeline(db, job_id),
            "codegen_tasks": _codegen_tasks(db, job_id),
            "gen": gen,
            "build": _latest_build(db, job_id),
            "can_ios_build": can_ios_build,
        },
    )


@router.post("/jobs/{job_id}/codemagic-build")
def jobs_codemagic_build(
    request: Request,
    job_id: uuid.UUID,
    csrf_token: str = Form(...),
    user: SessionData = Depends(require_user),
    db: Session = Depends(get_db),
) -> Response:
    if not csrf.verify(user.csrf_token, csrf_token):
        return Response("Invalid request (CSRF).", status_code=400)
    gen = db.scalar(select(GenerationResult).where(GenerationResult.job_id == job_id))
    if not (gen and gen.codemagic and gen.codemagic.get("application_id")):
        return RedirectResponse(f"/jobs/{job_id}", status_code=HTTP_303_SEE_OTHER)
    try:
        from iosforge.worker.run_job import run_codemagic_build

        run_codemagic_build.apply_async(args=[str(job_id)], queue="delivery")
        log.info("jobs.codemagic_build_requested", job_id=str(job_id))
    except Exception as exc:
        log.error("jobs.codemagic_build_enqueue_failed", job_id=str(job_id), error=str(exc))
    return RedirectResponse(f"/jobs/{job_id}", status_code=HTTP_303_SEE_OTHER)


@router.post("/jobs/{job_id}/rework")
def jobs_rework(
    request: Request,
    job_id: uuid.UUID,
    csrf_token: str = Form(...),
    instructions: str = Form(...),
    user: SessionData = Depends(require_user),
    db: Session = Depends(get_db),
) -> Response:
    if not csrf.verify(user.csrf_token, csrf_token):
        return Response("Invalid request (CSRF).", status_code=400)
    text = (instructions or "").strip()
    gen = db.scalar(select(GenerationResult).where(GenerationResult.job_id == job_id))
    if gen is None or not text:
        return RedirectResponse(f"/jobs/{job_id}", status_code=HTTP_303_SEE_OTHER)
    try:
        from iosforge.worker.run_job import rework_frontend

        rework_frontend.apply_async(args=[str(job_id), text], queue="codegen")
        log.info("jobs.rework_requested", job_id=str(job_id))
    except Exception as exc:
        log.error("jobs.rework_enqueue_failed", job_id=str(job_id), error=str(exc))
    return RedirectResponse(f"/jobs/{job_id}", status_code=HTTP_303_SEE_OTHER)


@router.post("/jobs/{job_id}/verify-web")
def jobs_verify_web(
    request: Request,
    job_id: uuid.UUID,
    csrf_token: str = Form(...),
    user: SessionData = Depends(require_user),
    db: Session = Depends(get_db),
) -> Response:
    if not csrf.verify(user.csrf_token, csrf_token):
        return Response("Invalid request (CSRF).", status_code=400)
    job = db.get(Job, job_id)
    gen = db.scalar(select(GenerationResult).where(GenerationResult.job_id == job_id))
    if job is None or gen is None or job.state not in {JobState.DONE, JobState.NEEDS_INPUT}:
        return RedirectResponse(f"/jobs/{job_id}", status_code=HTTP_303_SEE_OTHER)
    _enqueue_verify(job_id)
    log.info("jobs.verify_web_requested", job_id=str(job_id))
    return RedirectResponse(f"/jobs/{job_id}", status_code=HTTP_303_SEE_OTHER)


@router.get("/jobs/{job_id}/codemagic-build/logs")
def jobs_codemagic_logs(
    job_id: uuid.UUID,
    user: SessionData = Depends(require_user),
    db: Session = Depends(get_db),
) -> Response:
    build = _latest_build(db, job_id)
    if build is None:
        return Response("No build", status_code=404)
    return PlainTextResponse(build.log_text or build.message or "(no logs yet)")


@router.get("/jobs/{job_id}/codemagic-build/artifact")
def jobs_codemagic_artifact(
    job_id: uuid.UUID,
    i: int,
    user: SessionData = Depends(require_user),
    db: Session = Depends(get_db),
) -> Response:
    from iosforge.common.config import get_settings
    from iosforge.mvp import codemagic_build

    build = _latest_build(db, job_id)
    if build is None or i < 0 or i >= len(build.artifacts or []):
        return Response("Artifact not found", status_code=404)
    art = build.artifacts[i]
    url = art.get("url")
    if not url:
        return Response("Artifact has no URL", status_code=404)
    settings = get_settings()
    upstream = codemagic_build.download_artifact(
        settings, codemagic_build.resolve_token(settings), url
    )
    if upstream.status_code != 200:
        return Response(f"CodeMagic returned {upstream.status_code}", status_code=502)
    name = art.get("name") or f"artifact-{i}"
    return StreamingResponse(
        iter([upstream.content]),
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )


@router.post("/jobs/{job_id}/build")
def jobs_build(
    request: Request,
    job_id: uuid.UUID,
    csrf_token: str = Form(...),
    user: SessionData = Depends(require_user),
    db: Session = Depends(get_db),
) -> Response:
    if not csrf.verify(user.csrf_token, csrf_token):
        return Response("Invalid request (CSRF).", status_code=400)
    job = db.get(Job, job_id)
    if job is None:
        return Response("Not found", status_code=404)
    walk = db.scalar(select(WalkthroughResult).where(WalkthroughResult.job_id == job_id))
    gen = db.scalar(select(GenerationResult).where(GenerationResult.job_id == job_id))
    # Guard: analysis must be FINISHED (state DONE — not still WALKTHROUGH/ANALYSIS)
    # and no build may already exist. state==DONE also excludes an in-flight CODEGEN.
    if walk is None or not walk.screen_map or gen is not None or job.state != JobState.DONE:
        return RedirectResponse(f"/jobs/{job_id}", status_code=HTTP_303_SEE_OTHER)
    _enqueue_build(job_id)
    log.info("jobs.build_requested", job_id=str(job_id))
    return RedirectResponse(f"/jobs/{job_id}", status_code=HTTP_303_SEE_OTHER)


@router.get("/jobs/{job_id}/timeline")
def job_timeline(
    request: Request,
    job_id: uuid.UUID,
    user: SessionData = Depends(require_user),
    db: Session = Depends(get_db),
) -> Response:
    job = db.get(Job, job_id)
    if job is None:
        return Response("Not found", status_code=404)
    gen = db.scalar(select(GenerationResult).where(GenerationResult.job_id == job_id))
    return templates.TemplateResponse(
        request,
        "_timeline.html",
        {
            "job": job,
            "timeline": _timeline(db, job_id),
            "codegen_tasks": _codegen_tasks(db, job_id),
            "gen": gen,
        },
    )


@router.get("/jobs/{job_id}/artifacts")
def job_artifacts(
    request: Request,
    job_id: uuid.UUID,
    user: SessionData = Depends(require_user),
    db: Session = Depends(get_db),
) -> Response:
    job = db.get(Job, job_id)
    if job is None:
        return Response("Not found", status_code=404)
    walk = db.scalar(select(WalkthroughResult).where(WalkthroughResult.job_id == job_id))
    gen = db.scalar(
        select(GenerationResult)
        .where(GenerationResult.job_id == job_id)
        .order_by(GenerationResult.created_at.desc())
    )
    return templates.TemplateResponse(
        request,
        "job_artifacts.html",
        {"job": job, "user": user, "walk": walk, "gen": gen},
    )


@router.get("/jobs/{job_id}/artifacts/object")
def artifact_object(
    job_id: uuid.UUID,
    key: str,
    user: SessionData = Depends(require_user),
    storage: ArtifactStorage = Depends(get_storage),
) -> Response:
    # Authorization: the key MUST live under this job's prefix — prevents reading
    # another job's artifacts by passing an arbitrary key.
    if not key.startswith(f"jobs/{job_id}/"):
        return Response("Forbidden", status_code=403)
    try:
        data = storage.get(key)
    except Exception:
        return Response("Not found", status_code=404)
    filename = key.rsplit("/", 1)[-1]  # e.g. flutter_app.zip / 0000.png
    if key.endswith(".png"):
        media, disposition = "image/png", "inline"  # inline so the gallery renders
    elif key.endswith(".zip"):
        media, disposition = "application/zip", "attachment"
    else:
        media, disposition = "application/octet-stream", "attachment"
    return Response(
        content=data,
        media_type=media,
        headers={"Content-Disposition": f'{disposition}; filename="{filename}"'},
    )


def _timeline(db: Session, job_id: uuid.UUID) -> list[StageTimeline]:
    return list(
        db.scalars(
            select(StageTimeline)
            .where(StageTimeline.job_id == job_id)
            .order_by(StageTimeline.created_at.asc())
        ).all()
    )


def _codegen_tasks(db: Session, job_id: uuid.UUID) -> list[CodegenTask]:
    return list(
        db.scalars(
            select(CodegenTask).where(CodegenTask.job_id == job_id).order_by(CodegenTask.idx.asc())
        ).all()
    )


def _enqueue(job_id: uuid.UUID) -> None:
    try:
        from iosforge.worker.run_job import run_job

        run_job.apply_async(args=[str(job_id)], queue="codegen")
    except Exception as exc:  # broker down — Job stays QUEUED, surfaced in the UI
        log.error("jobs.enqueue_failed", job_id=str(job_id), error=str(exc))


def _enqueue_build(job_id: uuid.UUID) -> None:
    try:
        from iosforge.worker.run_job import build_frontend

        build_frontend.apply_async(args=[str(job_id)], queue="codegen")
    except Exception as exc:  # broker down — surfaced in the UI, build can be retried
        log.error("jobs.build_enqueue_failed", job_id=str(job_id), error=str(exc))


def _enqueue_verify(job_id: uuid.UUID) -> None:
    try:
        from iosforge.worker.run_job import reverify_web

        reverify_web.apply_async(args=[str(job_id)], queue="codegen")
    except Exception as exc:  # broker down — surfaced in the UI, verify can be retried
        log.error("jobs.verify_web_enqueue_failed", job_id=str(job_id), error=str(exc))


def _latest_build(db: Session, job_id: uuid.UUID) -> CodemagicBuild | None:
    return db.scalar(
        select(CodemagicBuild)
        .where(CodemagicBuild.job_id == job_id)
        .order_by(CodemagicBuild.created_at.desc())
    )


def _new_error(request: Request, user: SessionData, message: str, *, status: int) -> Response:
    return templates.TemplateResponse(
        request, "job_new.html", {"user": user, "error": message}, status_code=status
    )
