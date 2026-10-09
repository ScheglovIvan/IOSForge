"""Job routes: list, upload APK, detail, polling fragment, artifacts (SPEC §7)."""

from __future__ import annotations

import json
import re
import uuid
from datetime import UTC, datetime
from urllib.parse import quote_plus

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.responses import PlainTextResponse, RedirectResponse, Response
from starlette.status import HTTP_303_SEE_OTHER

from iosforge.admin import archive_validate, csrf, thumbs
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
        source_app_metadata={
            "app_id": parsed.app_id,
            "country": parsed.country,
            "build_profile": "test",
        },
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
    storage: ArtifactStorage = Depends(get_storage),
    signed: str = "",
    sign_error: str = "",
) -> Response:
    from iosforge.mvp import asc_credentials

    job = db.get(Job, job_id)
    if job is None:
        return Response("Not found", status_code=404)
    gen = db.scalar(select(GenerationResult).where(GenerationResult.job_id == job_id))
    scope_status = (job.source_app_metadata or {}).get("scope_status")
    from iosforge.worker.swiftui_build import build_refusal
    from iosforge.worker.swiftui_tasks import latest_xcode_build

    xcode_build = latest_xcode_build(db, job_id)
    can_xcode_delivery = xcode_build is not None or _has_sources(storage, job_id)
    can_build = job.state in (JobState.DONE, JobState.FAILED) and build_refusal(db, job) is None
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
            "can_ios_build": can_xcode_delivery or _latest_build(db, job_id) is not None,
            "can_xcode_delivery": can_xcode_delivery,
            "can_build": can_build,
            "xcode_build": xcode_build,
            "store_slides": _store_slides(job_id),
            "ipad_slides": _ipad_slides(job_id),
            "icon_versions": _icon_versions(job_id),
            "store_listing": _store_listing(job_id),
            "store_categories": _store_categories(),
            "signing": asc_credentials.status(get_settings(), str(job_id)),
            "signed_ok": signed == "1",
            "sign_error": sign_error,
            "scope_status": scope_status,
            "scope": _load_scope(storage, job_id, scope_status),
            "capability_modules": _capability_modules(),
        },
    )


@router.post("/jobs/{job_id}/rework")
def jobs_rework(
    request: Request,
    job_id: uuid.UUID,
    csrf_token: str = Form(...),
    instructions: str = Form(...),
    user: SessionData = Depends(require_user),
    db: Session = Depends(get_db),
    storage: ArtifactStorage = Depends(get_storage),
) -> Response:
    if not csrf.verify(user.csrf_token, csrf_token):
        return Response("Invalid request (CSRF).", status_code=400)
    text = (instructions or "").strip()
    gen = db.scalar(select(GenerationResult).where(GenerationResult.job_id == job_id))
    if gen is None or not text:
        return RedirectResponse(f"/jobs/{job_id}", status_code=HTTP_303_SEE_OTHER)
    from iosforge.worker.swiftui_tasks import SOURCES_NAME

    if gen.sources_key.endswith(SOURCES_NAME):
        return _enqueue_swiftui_round(db, storage, job_id, text, None)
    log.info("jobs.rework_legacy_unsupported", job_id=str(job_id), sources=gen.sources_key)
    return Response("Rework is not available for legacy Flutter jobs.", status_code=409)


@router.post("/jobs/{job_id}/extend-scope")
def jobs_extend_scope(
    request: Request,
    job_id: uuid.UUID,
    csrf_token: str = Form(...),
    screens: str = Form(...),
    user: SessionData = Depends(require_user),
    db: Session = Depends(get_db),
    storage: ArtifactStorage = Depends(get_storage),
) -> Response:
    """Post-MVP scope extension of a SwiftUI app: add screens (ids, comma/space separated)."""
    if not csrf.verify(user.csrf_token, csrf_token):
        return Response("Invalid request (CSRF).", status_code=400)
    ids = [part for part in re.split(r"[\s,]+", screens or "") if part]
    gen = db.scalar(select(GenerationResult).where(GenerationResult.job_id == job_id))
    from iosforge.worker.swiftui_tasks import SOURCES_NAME

    if not ids or gen is None or not gen.sources_key.endswith(SOURCES_NAME):
        return RedirectResponse(f"/jobs/{job_id}", status_code=HTTP_303_SEE_OTHER)
    return _enqueue_swiftui_round(db, storage, job_id, "", ids)


def _enqueue_swiftui_round(
    db: Session,
    storage: ArtifactStorage,
    job_id: uuid.UUID,
    instructions: str,
    add_screens: list[str] | None,
) -> Response:
    from iosforge.worker.swiftui_rework import rework_refusal, rework_swiftui
    from iosforge.worker.swiftui_tasks import XCODE_QUEUE

    job = db.get(Job, job_id)
    if job is None:
        return Response("Not found", status_code=404)
    refusal = rework_refusal(db, job, storage)
    if refusal:
        return Response(f"Rework is not possible now: {refusal}.", status_code=409)
    try:
        rework_swiftui.apply_async(args=[str(job_id), instructions, add_screens], queue=XCODE_QUEUE)
        log.info("jobs.swiftui_round_requested", job_id=str(job_id), screens=add_screens)
    except Exception as exc:
        log.error("jobs.swiftui_round_enqueue_failed", job_id=str(job_id), error=str(exc))
    return RedirectResponse(f"/jobs/{job_id}", status_code=HTTP_303_SEE_OTHER)


@router.post("/jobs/{job_id}/build-settings")
def jobs_build_settings(
    request: Request,
    job_id: uuid.UUID,
    csrf_token: str = Form(...),
    build_profile: str = Form("test"),
    bundle_id: str = Form(""),
    app_name: str = Form(""),
    apphud_api_key: str = Form(""),
    tenjin_api_key: str = Form(""),
    appstore_apple_id: str = Form(""),
    user: SessionData = Depends(require_user),
    db: Session = Depends(get_db),
) -> Response:
    """Save the build profile / bundle id / display name / SDK keys, then rebuild.

    A job without a generated app is built; a SwiftUI app is re-archived (delivery
    re-renders the contract with the new identity and keys, no model round needed).
    """
    if not csrf.verify(user.csrf_token, csrf_token):
        return Response("Invalid request (CSRF).", status_code=400)
    job = db.get(Job, job_id)
    if job is None:
        return Response("Not found", status_code=404)
    profile = build_profile if build_profile in ("real", "store") else "test"
    bundle_id = (bundle_id or "").strip()
    app_name = (app_name or "").strip()
    appstore_apple_id = (appstore_apple_id or "").strip()
    apphud_api_key = (apphud_api_key or "").strip()
    tenjin_api_key = (tenjin_api_key or "").strip()
    if bundle_id and not re.fullmatch(r"[A-Za-z0-9.]{1,80}", bundle_id):
        return Response("Invalid bundle id.", status_code=400)
    if app_name and not re.fullmatch(r"[\w .\-]{1,50}", app_name):
        return Response("Invalid app name.", status_code=400)
    # Apphud publishable SDK key (appstr_… / app_…). Apps get one key each, so it is
    # stored per job; empty falls back to the shared key in the gitignored secrets file.
    if apphud_api_key and not re.fullmatch(r"[A-Za-z0-9_\-]{8,128}", apphud_api_key):
        return Response("Invalid Apphud SDK key.", status_code=400)
    # Tenjin iOS SDK key (32-char uppercase alphanumeric), also one per app.
    if tenjin_api_key and not re.fullmatch(r"[A-Za-z0-9]{16,64}", tenjin_api_key):
        return Response("Invalid Tenjin SDK key.", status_code=400)
    if appstore_apple_id and not re.fullmatch(r"[0-9]{5,15}", appstore_apple_id):
        return Response("Invalid App Store Apple ID (numbers only).", status_code=400)

    meta = dict(job.source_app_metadata or {})
    meta["build_profile"] = profile
    # A custom iOS bundle id is written into the binary for real and store builds.
    meta["override_bundle_id"] = bundle_id if profile in ("real", "store") else ""
    meta["override_app_name"] = app_name
    meta["apphud_api_key"] = apphud_api_key
    meta["tenjin_api_key"] = tenjin_api_key
    meta["appstore_apple_id"] = appstore_apple_id
    job.source_app_metadata = meta  # reassign so SQLAlchemy persists the JSONB change
    db.commit()

    gen = db.scalar(select(GenerationResult).where(GenerationResult.job_id == job_id))
    try:
        from iosforge.worker.swiftui_build import build_refusal
        from iosforge.worker.swiftui_tasks import DELIVERABLE_STATES, SOURCES_NAME, queue_delivery

        if gen is None:
            if job.state == JobState.DONE and build_refusal(db, job) is None:
                _enqueue_build(job_id)
        elif gen.sources_key.endswith(SOURCES_NAME) and job.state in DELIVERABLE_STATES:
            queue_delivery(db, job)
        log.info(
            "jobs.build_settings_saved",
            job_id=str(job_id),
            profile=profile,
            apphud_key_set=bool(apphud_api_key),  # never log the keys themselves
            tenjin_key_set=bool(tenjin_api_key),
        )
    except Exception as exc:
        log.error("jobs.build_settings_enqueue_failed", job_id=str(job_id), error=str(exc))
    return RedirectResponse(f"/jobs/{job_id}", status_code=HTTP_303_SEE_OTHER)


@router.post("/jobs/{job_id}/app-icon")
def jobs_app_icon(
    request: Request,
    job_id: uuid.UUID,
    csrf_token: str = Form(...),
    user: SessionData = Depends(require_user),
    db: Session = Depends(get_db),
) -> Response:
    """Draw the clone's app icon from the source icon, in our palette."""
    if not csrf.verify(user.csrf_token, csrf_token):
        return Response("Invalid request (CSRF).", status_code=400)
    try:
        from iosforge.worker.run_job import generate_app_icon

        generate_app_icon.apply_async(args=[str(job_id)], queue="codegen")
        log.info("jobs.app_icon_requested", job_id=str(job_id))
    except Exception as exc:
        log.error("jobs.app_icon_enqueue_failed", job_id=str(job_id), error=str(exc))
    return RedirectResponse(f"/jobs/{job_id}?icon=1#t-assets", status_code=HTTP_303_SEE_OTHER)


def _store_categories() -> tuple[str, ...]:
    """The App Store category ids the auto-fill picker offers."""
    from iosforge.mvp.asc_api import CATEGORY_IDS

    return CATEGORY_IDS


def _store_listing(job_id: uuid.UUID) -> dict[str, object] | None:
    """The drafted App Store listing for a job, with per-field length usage.

    Returns ``None`` when nothing has been generated yet, so the template can show the
    generate button instead of an empty form.
    """
    from iosforge.mvp import store_listing
    from iosforge.storage.client import S3ArtifactStorage, build_key

    key = build_key(job_id=str(job_id), kind="store_listing", name="listing.json")
    storage = S3ArtifactStorage()
    if not storage.exists(key):
        return None
    try:
        listing = store_listing.from_dict(json.loads(storage.get(key)))
    except Exception:
        return None
    data = listing.to_dict()
    data["lengths"] = {
        name: {"used": used, "max": limit, "over": used > limit}
        for name, (used, limit) in listing.field_lengths.items()
    }
    return data


@router.post("/jobs/{job_id}/store-listing/generate")
def jobs_store_listing_generate(
    request: Request,
    job_id: uuid.UUID,
    csrf_token: str = Form(...),
    subtitle: str = Form(""),
    keywords_seed: str = Form(""),
    lead: str = Form(""),
    user: SessionData = Depends(require_user),
) -> Response:
    """Draft the store listing from the app's spec (regeneration is safe to repeat)."""
    if not csrf.verify(user.csrf_token, csrf_token):
        return Response("Invalid request (CSRF).", status_code=400)
    seed = [w.strip() for w in keywords_seed.split(",") if w.strip()]
    try:
        from iosforge.worker.run_job import generate_store_listing

        generate_store_listing.apply_async(
            args=[str(job_id), subtitle.strip(), seed, lead.strip()], queue="codegen"
        )
        log.info("jobs.store_listing_requested", job_id=str(job_id))
    except Exception as exc:
        log.error("jobs.store_listing_enqueue_failed", job_id=str(job_id), error=str(exc))
    return RedirectResponse(f"/jobs/{job_id}?listing=1#t-store", status_code=HTTP_303_SEE_OTHER)


@router.post("/jobs/{job_id}/store-listing/save")
def jobs_store_listing_save(
    request: Request,
    job_id: uuid.UUID,
    csrf_token: str = Form(...),
    app_name: str = Form(""),
    subtitle: str = Form(""),
    keywords: str = Form(""),
    promotional_text: str = Form(""),
    description: str = Form(""),
    support_url: str = Form(""),
    marketing_url: str = Form(""),
    user: SessionData = Depends(require_user),
) -> Response:
    """Persist operator edits to the drafted listing, so the wording is theirs to tune."""
    if not csrf.verify(user.csrf_token, csrf_token):
        return Response("Invalid request (CSRF).", status_code=400)
    from iosforge.mvp import store_listing
    from iosforge.storage.client import S3ArtifactStorage, build_key

    key = build_key(job_id=str(job_id), kind="store_listing", name="listing.json")
    storage = S3ArtifactStorage()
    base = (
        store_listing.from_dict(json.loads(storage.get(key))).to_dict()
        if storage.exists(key)
        else {"privacy_policy_url": "", "terms_url": store_listing.APPLE_EULA_URL, "warnings": []}
    )
    base.update(
        {
            "app_name": app_name.strip(),
            "subtitle": subtitle.strip(),
            "keywords": keywords.strip(),
            "promotional_text": promotional_text.strip(),
            "description": description.strip(),
            "support_url": support_url.strip(),
            "marketing_url": marketing_url.strip(),
        }
    )
    storage.put(
        key,
        json.dumps(base, indent=2, ensure_ascii=False).encode("utf-8"),
        content_type="application/json",
    )
    log.info("jobs.store_listing_saved", job_id=str(job_id))
    return RedirectResponse(f"/jobs/{job_id}?saved=1#t-store", status_code=HTTP_303_SEE_OTHER)


@router.post("/jobs/{job_id}/store-metadata/autofill")
def jobs_store_metadata_autofill(
    request: Request,
    job_id: uuid.UUID,
    csrf_token: str = Form(...),
    primary_category: str = Form("UTILITIES"),
    secondary_category: str = Form(""),
    user: SessionData = Depends(require_user),
) -> Response:
    """Fill category, content rights, age rating and support URL in App Store Connect."""
    if not csrf.verify(user.csrf_token, csrf_token):
        return Response("Invalid request (CSRF).", status_code=400)
    try:
        from iosforge.worker.run_job import autofill_store_metadata

        autofill_store_metadata.apply_async(
            args=[str(job_id), primary_category.strip(), secondary_category.strip()],
            queue="codegen",
        )
        log.info("jobs.store_metadata_autofill_requested", job_id=str(job_id))
    except Exception as exc:
        log.error("jobs.store_metadata_autofill_failed", job_id=str(job_id), error=str(exc))
    return RedirectResponse(f"/jobs/{job_id}?autofill=1#t-store", status_code=HTTP_303_SEE_OTHER)


@router.post("/jobs/{job_id}/store-metadata/review-contact")
def jobs_store_review_contact(
    request: Request,
    job_id: uuid.UUID,
    csrf_token: str = Form(...),
    first_name: str = Form(...),
    last_name: str = Form(...),
    phone: str = Form(...),
    email: str = Form(...),
    notes: str = Form(""),
    user: SessionData = Depends(require_user),
) -> Response:
    """Write the App Review contact (name, phone, email) to App Store Connect."""
    if not csrf.verify(user.csrf_token, csrf_token):
        return Response("Invalid request (CSRF).", status_code=400)
    try:
        from iosforge.worker.run_job import set_review_contact

        set_review_contact.apply_async(
            args=[
                str(job_id),
                first_name.strip(),
                last_name.strip(),
                phone.strip(),
                email.strip(),
                notes.strip(),
            ],
            queue="codegen",
        )
        log.info("jobs.review_contact_requested", job_id=str(job_id))
    except Exception as exc:
        log.error("jobs.review_contact_failed", job_id=str(job_id), error=str(exc))
    return RedirectResponse(f"/jobs/{job_id}?contact=1#t-store", status_code=HTTP_303_SEE_OTHER)


@router.post("/jobs/{job_id}/store-listing/push")
def jobs_store_listing_push(
    request: Request,
    job_id: uuid.UUID,
    csrf_token: str = Form(...),
    user: SessionData = Depends(require_user),
) -> Response:
    """Send the drafted listing to App Store Connect via the job's own API key."""
    if not csrf.verify(user.csrf_token, csrf_token):
        return Response("Invalid request (CSRF).", status_code=400)
    try:
        from iosforge.worker.run_job import push_store_listing

        push_store_listing.apply_async(args=[str(job_id)], queue="codegen")
        log.info("jobs.store_listing_push_requested", job_id=str(job_id))
    except Exception as exc:
        log.error("jobs.store_listing_push_failed", job_id=str(job_id), error=str(exc))
    return RedirectResponse(f"/jobs/{job_id}?pushed=1#t-store", status_code=HTTP_303_SEE_OTHER)


_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


def _download_name(db: Session, job_id: uuid.UUID, suffix: str) -> str:
    """A filename an operator can recognise in their Downloads folder."""
    job = db.get(Job, job_id)
    meta = (job.source_app_metadata or {}) if job else {}
    raw = str(meta.get("override_app_name") or meta.get("override_bundle_id") or "app")
    slug = _SAFE_NAME.sub("-", raw).strip("-") or "app"
    return f"{slug}-{suffix}"


@router.get("/jobs/{job_id}/store-assets.zip")
def jobs_store_assets_zip(
    job_id: uuid.UUID,
    device: str = "iphone",
    size: str = "",
    user: SessionData = Depends(require_user),
    db: Session = Depends(get_db),
) -> Response:
    """One device's whole slide set in a single archive, at full resolution.

    App Store Connect takes a listing's screenshots as a batch and keeps a separate
    set per device class, so the useful unit is one set, not one slide. Nothing is
    re-encoded — these are the exact PNGs the pipeline produced.
    """
    import io as _io
    import zipfile

    from iosforge.storage.client import S3ArtifactStorage

    tablet = device.lower() == "ipad"
    keys = _ipad_slides(job_id) if tablet else _store_slides(job_id)
    if not keys:
        return Response("No slides yet", status_code=404)
    # App Store Connect validates the pixel size against the slot the operator drops
    # the files into, and rejects anything else. `size` produces the set at another
    # slot's dimensions so a listing can be filled without regenerating artwork.
    target = DEVICE_SIZES.get(size) if size else None
    storage = S3ArtifactStorage()
    buffer = _io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_STORED) as archive:
        for key in keys:
            try:
                archive.writestr(key.rsplit("/", 1)[-1], slide_for_slot(storage.get(key), target))
            except Exception as exc:
                log.warning("jobs.slide_zip_skipped", key=key, error=str(exc))
    suffix = "ipad-screenshots.zip" if tablet else "screenshots.zip"
    if target:
        suffix = f"{target[0]}x{target[1]}-{suffix}"
    name = _download_name(db, job_id, suffix)
    return Response(
        content=buffer.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )


def _icon_key(job_id: uuid.UUID) -> str:
    from iosforge.mvp.app_icon import ICON_NAME
    from iosforge.storage.client import build_key

    return build_key(job_id=str(job_id), kind="app_icon", name=ICON_NAME)


def _icon_versions(job_id: uuid.UUID) -> list[dict[str, int | str | None]]:
    """Every icon ever generated for this job, newest first.

    Each run writes the same key, so the bucket's versioning already holds the
    history — no separate candidate namespace to keep in step. Delete markers have
    no size and are skipped so a cleared icon does not show as a blank candidate.
    """
    from iosforge.storage.client import S3ArtifactStorage

    try:
        versions = S3ArtifactStorage().list_versions(_icon_key(job_id))
    except Exception:
        return []
    rows = [
        {
            "version_id": v.version_id,
            "is_latest": v.is_latest,
            "size": v.size,
            "at": v.last_modified.strftime("%d.%m %H:%M") if v.last_modified else "",
        }
        for v in versions
        if v.size > 0
    ]
    rows.sort(key=lambda r: (not r["is_latest"],))
    return rows


def _deleted(request: Request, job_id: uuid.UUID, ok: bool = True) -> Response:
    """Answer a delete: 204 for fetch, a redirect for a plain form post.

    The admin removes the tile itself, so a fetch caller needs no body; a browser
    without scripting still gets the page back.
    """
    if request.headers.get("x-requested-with") == "fetch":
        return Response(status_code=204 if ok else 409)
    return RedirectResponse(f"/jobs/{job_id}#t-store", status_code=HTTP_303_SEE_OTHER)


@router.post("/jobs/{job_id}/app-icon/delete")
def jobs_app_icon_delete(
    request: Request,
    job_id: uuid.UUID,
    csrf_token: str = Form(...),
    version_id: str = Form(...),
    user: SessionData = Depends(require_user),
) -> Response:
    """Discard one generated icon.

    Refuses to remove the last one: an app with no icon ships the scaffold's
    placeholder icon, which is never what deleting a bad variant was meant to achieve.
    """
    if not csrf.verify(user.csrf_token, csrf_token):
        return Response("Invalid request (CSRF).", status_code=400)
    from iosforge.storage.client import S3ArtifactStorage

    if len(_icon_versions(job_id)) <= 1:
        return _deleted(request, job_id, ok=False)
    try:
        S3ArtifactStorage().delete_version(_icon_key(job_id), version_id)
        log.info("jobs.app_icon_deleted", job_id=str(job_id), version_id=version_id)
    except Exception as exc:
        log.error("jobs.app_icon_delete_failed", job_id=str(job_id), error=str(exc))
        return _deleted(request, job_id, ok=False)
    return _deleted(request, job_id)


@router.post("/jobs/{job_id}/store-assets/slide/delete")
def jobs_store_slide_delete(
    request: Request,
    job_id: uuid.UUID,
    csrf_token: str = Form(...),
    index: str = Form(...),
    user: SessionData = Depends(require_user),
    storage: ArtifactStorage = Depends(get_storage),
) -> Response:
    """Drop one listing slide, phone or tablet.

    A soft delete: the bucket keeps the object's history, so a slide removed by
    mistake is still recoverable from storage even though the listing stops
    showing it.
    """
    if not csrf.verify(user.csrf_token, csrf_token):
        return Response("Invalid request (CSRF).", status_code=400)
    name = _slide_name(index)
    if name is None:
        return Response("Invalid slide index.", status_code=400)
    key = build_key(job_id=str(job_id), kind="store_assets", name=name)
    try:
        storage.delete(key)
        log.info("jobs.store_slide_deleted", job_id=str(job_id), slide=name)
    except Exception as exc:
        log.error("jobs.store_slide_delete_failed", job_id=str(job_id), error=str(exc))
        return _deleted(request, job_id, ok=False)
    return _deleted(request, job_id)


@router.post("/jobs/{job_id}/app-icon/select")
def jobs_app_icon_select(
    job_id: uuid.UUID,
    csrf_token: str = Form(...),
    version_id: str = Form(...),
    user: SessionData = Depends(require_user),
) -> Response:
    """Promote a previously generated icon back to being the current one.

    Written as a new version rather than by deleting what came after, so the
    history stays complete and the choice itself is reversible.
    """
    if not csrf.verify(user.csrf_token, csrf_token):
        return Response("Invalid request (CSRF).", status_code=400)
    from iosforge.storage.client import S3ArtifactStorage

    storage = S3ArtifactStorage()
    key = _icon_key(job_id)
    try:
        storage.put(key, storage.get(key, version_id=version_id), content_type="image/png")
        log.info("jobs.app_icon_selected", job_id=str(job_id), version_id=version_id)
    except Exception as exc:
        log.error("jobs.app_icon_select_failed", job_id=str(job_id), error=str(exc))
    return RedirectResponse(f"/jobs/{job_id}#t-store", status_code=HTTP_303_SEE_OTHER)


@router.get("/jobs/{job_id}/app-icon.png")
def jobs_app_icon_image(
    request: Request,
    job_id: uuid.UUID,
    w: int | None = None,
    v: str | None = None,
    download: int = 0,
    user: SessionData = Depends(require_user),
    db: Session = Depends(get_db),
) -> Response:
    """Serve the generated icon so the operator can judge it before rebuilding.

    ``no-cache`` rather than ``no-store``: the browser still keeps the bytes and
    only asks whether they changed, so the page's periodic refresh costs a 304
    instead of re-downloading a megabyte — while a re-rolled icon still appears
    immediately, because regenerating changes the ETag.
    """
    from iosforge.mvp.app_icon import ICON_NAME
    from iosforge.storage.client import S3ArtifactStorage, build_key

    storage = S3ArtifactStorage()
    key = build_key(job_id=str(job_id), kind="app_icon", name=ICON_NAME)
    try:
        if not storage.exists(key):
            return Response("No icon yet", status_code=404)
        data = storage.get(key, version_id=v) if v else storage.get(key)
    except Exception:
        return Response("No icon yet", status_code=404)

    width = thumbs.clamp_width(w) if w else 0
    # A past version is a different image under the same key, so it needs its own
    # cache identity or the current icon's preview would be served for it.
    cache_key = f"{key}@{v}" if v else key
    tag = f'"{thumbs.etag(cache_key, width, len(data))}"'
    headers = {"Cache-Control": "private, no-cache", "ETag": tag}
    if request.headers.get("if-none-match") == tag:
        return Response(status_code=304, headers=headers)
    if width:
        return Response(
            content=thumbs.thumbnail(cache_key, data, width),
            media_type="image/jpeg",
            headers=headers,
        )
    if download:
        # The App Store icon: 1024x1024, opaque, unrounded — exactly the bytes the
        # pipeline normalised, never a preview.
        name = _download_name(db, job_id, "icon-1024.png")
        return Response(
            content=data,
            media_type="image/png",
            headers={"Content-Disposition": f'attachment; filename="{name}"'},
        )
    return Response(data, media_type="image/png", headers=headers)


@router.post("/jobs/{job_id}/ipad-slides")
def jobs_ipad_slides(
    job_id: uuid.UUID,
    csrf_token: str = Form(...),
    user: SessionData = Depends(require_user),
) -> Response:
    """Re-frame the leading slides for the iPad listing."""
    if not csrf.verify(user.csrf_token, csrf_token):
        return Response("Invalid request (CSRF).", status_code=400)
    try:
        from iosforge.worker.run_job import generate_ipad_slides

        generate_ipad_slides.apply_async(args=[str(job_id)], queue="codegen")
        log.info("jobs.ipad_slides_requested", job_id=str(job_id))
    except Exception as exc:
        log.error("jobs.ipad_slides_enqueue_failed", job_id=str(job_id), error=str(exc))
    return RedirectResponse(f"/jobs/{job_id}#t-store", status_code=HTTP_303_SEE_OTHER)


@router.post("/jobs/{job_id}/store-assets")
def jobs_store_assets(
    request: Request,
    job_id: uuid.UUID,
    csrf_token: str = Form(...),
    user: SessionData = Depends(require_user),
    db: Session = Depends(get_db),
) -> Response:
    """Render the App Store listing screenshots from the app's own generated screens."""
    if not csrf.verify(user.csrf_token, csrf_token):
        return Response("Invalid request (CSRF).", status_code=400)
    gen = db.scalar(select(GenerationResult).where(GenerationResult.job_id == job_id))
    if gen is None:
        return RedirectResponse(f"/jobs/{job_id}", status_code=HTTP_303_SEE_OTHER)
    try:
        from iosforge.worker.run_job import generate_store_assets

        generate_store_assets.apply_async(args=[str(job_id)], queue="codegen")
        log.info("jobs.store_assets_requested", job_id=str(job_id))
    except Exception as exc:
        log.error("jobs.store_assets_enqueue_failed", job_id=str(job_id), error=str(exc))
    return RedirectResponse(f"/jobs/{job_id}", status_code=HTTP_303_SEE_OTHER)


@router.post("/jobs/{job_id}/store-assets/slide")
def jobs_store_slide(
    request: Request,
    job_id: uuid.UUID,
    csrf_token: str = Form(...),
    index: int = Form(...),
    instructions: str = Form(""),
    user: SessionData = Depends(require_user),
    db: Session = Depends(get_db),
) -> Response:
    """Refine one listing slide in place from its existing HTML, per an instruction."""
    if not csrf.verify(user.csrf_token, csrf_token):
        return Response("Invalid request (CSRF).", status_code=400)
    if index < 1 or index > 10:
        return Response("Invalid slide.", status_code=400)
    try:
        from iosforge.worker.run_job import regenerate_store_slide

        regenerate_store_slide.apply_async(
            args=[str(job_id), index, (instructions or "").strip()], queue="codegen"
        )
        log.info("jobs.store_slide_requested", job_id=str(job_id), index=index)
    except Exception as exc:
        log.error("jobs.store_slide_enqueue_failed", job_id=str(job_id), error=str(exc))
    return RedirectResponse(f"/jobs/{job_id}", status_code=HTTP_303_SEE_OTHER)


@router.post("/jobs/{job_id}/scope")
async def jobs_scope_approve(
    request: Request,
    job_id: uuid.UUID,
    csrf_token: str = Form(...),
    scope_mode: str = Form("core"),
    notes: str = Form(""),
    user: SessionData = Depends(require_user),
    db: Session = Depends(get_db),
    storage: ArtifactStorage = Depends(get_storage),
) -> Response:
    if not csrf.verify(user.csrf_token, csrf_token):
        return Response("Invalid request (CSRF).", status_code=400)
    job = db.get(Job, job_id)
    meta = dict(job.source_app_metadata or {}) if job is not None else {}
    if job is None or job.state != JobState.NEEDS_INPUT or meta.get("scope_status") != "proposed":
        return RedirectResponse(f"/jobs/{job_id}#t-scope", status_code=HTTP_303_SEE_OTHER)

    from iosforge.mvp import feasibility

    scope = feasibility.load_scope(storage, str(job_id))
    form = await request.form()
    for screen in scope.screens:
        screen.include = f"include_{screen.screen_id}" in form
    feasibility.confirm_routing(scope, {k: str(v) for k, v in form.items()})
    scope.scope_mode = "full" if scope_mode == "full" else "core"
    scope.notes = notes
    scope.status = "approved"
    scope.approved_by = user.username
    approved_at = datetime.now(UTC)
    scope.approved_at = approved_at
    scope.recount()
    ref = feasibility.save_scope(storage, str(job_id), scope)

    meta["scope_status"] = "approved"
    meta["scope_version_id"] = ref.version_id
    meta["scope_approved_at"] = approved_at.isoformat()
    meta["scope_approved_by"] = user.username
    job.source_app_metadata = meta
    job.state = JobState.CODEGEN
    db.commit()

    _enqueue_build(job_id)
    log.info(
        "jobs.scope_approved",
        job_id=str(job_id),
        included=scope.counts.included,
        total=scope.counts.total,
        scope_mode=scope.scope_mode,
    )
    return RedirectResponse(f"/jobs/{job_id}#t-scope", status_code=HTTP_303_SEE_OTHER)


def _has_sources(storage: ArtifactStorage, job_id: uuid.UUID) -> bool:
    from iosforge.worker.swiftui_tasks import sources_key

    try:
        return storage.exists(sources_key(str(job_id)))
    except Exception as exc:
        log.warning("jobs.sources_check_failed", job_id=str(job_id), error=str(exc))
        return False


@router.post("/jobs/{job_id}/xcode-delivery")
def jobs_xcode_delivery(
    request: Request,
    job_id: uuid.UUID,
    csrf_token: str = Form(...),
    user: SessionData = Depends(require_user),
    db: Session = Depends(get_db),
    storage: ArtifactStorage = Depends(get_storage),
) -> Response:
    if not csrf.verify(user.csrf_token, csrf_token):
        return Response("Invalid request (CSRF).", status_code=400)
    from iosforge.worker.swiftui_tasks import DELIVERABLE_STATES, queue_delivery

    job = db.get(Job, job_id)
    if job is None:
        return Response("Not found", status_code=404)
    if job.state not in DELIVERABLE_STATES:
        return Response(f"Delivery needs a finished job (state {job.state}).", status_code=409)
    if not _has_sources(storage, job_id):
        return Response("No generated SwiftUI sources for this job.", status_code=409)
    try:
        build = queue_delivery(db, job)
    except Exception as exc:
        log.error("jobs.xcode_delivery_enqueue_failed", job_id=str(job_id), error=str(exc))
        return RedirectResponse(f"/jobs/{job_id}", status_code=HTTP_303_SEE_OTHER)
    if build is None:
        return Response("A native delivery is already running.", status_code=409)
    log.info("jobs.xcode_delivery_requested", job_id=str(job_id), build_id=str(build.id))
    return RedirectResponse(f"/jobs/{job_id}", status_code=HTTP_303_SEE_OTHER)


@router.get("/jobs/{job_id}/xcode-delivery/logs")
def jobs_xcode_delivery_logs(
    job_id: uuid.UUID,
    user: SessionData = Depends(require_user),
    db: Session = Depends(get_db),
) -> Response:
    from iosforge.worker.swiftui_tasks import latest_xcode_build

    build = latest_xcode_build(db, job_id)
    if build is None:
        return Response("No build", status_code=404)
    return PlainTextResponse(build.log_text or build.message or "(no logs yet)")


@router.get("/jobs/{job_id}/xcode-delivery/ipa")
def jobs_xcode_delivery_ipa(
    job_id: uuid.UUID,
    user: SessionData = Depends(require_user),
    db: Session = Depends(get_db),
    storage: ArtifactStorage = Depends(get_storage),
) -> Response:
    from iosforge.worker.swiftui_tasks import latest_xcode_build

    build = latest_xcode_build(db, job_id)
    if build is None or not build.ipa_key:
        return Response("No IPA", status_code=404)
    name = build.ipa_key.rsplit("/", 1)[-1]
    return Response(
        storage.get(build.ipa_key),
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )


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
    from iosforge.worker.swiftui_build import build_refusal

    refusal = build_refusal(db, job)
    if refusal or job.state not in (JobState.DONE, JobState.FAILED):
        log.info("jobs.build_refused", job_id=str(job_id), reason=refusal or str(job.state))
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
    request: Request,
    job_id: uuid.UUID,
    key: str,
    w: int | None = None,
    download: int = 0,
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
    filename = key.rsplit("/", 1)[-1]

    # A gallery tile shows a 1290x2796 slide at ~120px. Serving the original for
    # that pulled tens of megabytes per tab; `w` asks for a cached downscale
    # instead. Artifacts are immutable once written, so they can be cached hard —
    # a regenerated slide arrives under a new ETag.
    if key.endswith(".png") and w:
        width = thumbs.clamp_width(w)
        tag = f'"{thumbs.etag(key, width, len(data))}"'
        if request.headers.get("if-none-match") == tag:
            return Response(status_code=304)
        return Response(
            content=thumbs.thumbnail(key, data, width),
            media_type="image/jpeg",
            headers={"Cache-Control": "private, max-age=604800", "ETag": tag},
        )

    if key.endswith(".png"):
        # inline so the gallery renders; `download` asks for the file instead
        media = "image/png"
        disposition = "attachment" if download else "inline"
    elif key.endswith(".zip"):
        media, disposition = "application/zip", "attachment"
    else:
        media, disposition = "application/octet-stream", "attachment"
    headers = {"Content-Disposition": f'{disposition}; filename="{filename}"'}
    if key.endswith(".png"):
        headers["Cache-Control"] = "private, max-age=604800"
        headers["ETag"] = f'"{thumbs.etag(key, 0, len(data))}"'
        if request.headers.get("if-none-match") == headers["ETag"]:
            return Response(status_code=304)
    return Response(content=data, media_type=media, headers=headers)


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


def _capability_modules() -> list[str]:
    from iosforge.mvp import capability_registry

    return list(capability_registry.MODULES)


def _load_scope(
    storage: ArtifactStorage, job_id: uuid.UUID, scope_status: str | None
) -> object | None:
    if scope_status not in {"proposed", "approved"}:
        return None
    try:
        from iosforge.mvp import feasibility

        return feasibility.load_scope(storage, str(job_id))
    except Exception as exc:
        log.warning("jobs.scope_load_failed", job_id=str(job_id), error=str(exc))
        return None


def _enqueue(job_id: uuid.UUID) -> None:
    try:
        from iosforge.worker.run_job import run_job

        run_job.apply_async(args=[str(job_id)], queue="codegen")
    except Exception as exc:  # broker down — Job stays QUEUED, surfaced in the UI
        log.error("jobs.enqueue_failed", job_id=str(job_id), error=str(exc))


def _enqueue_build(job_id: uuid.UUID) -> None:
    try:
        from iosforge.worker.swiftui_build import enqueue_build

        enqueue_build(str(job_id))
    except Exception as exc:  # broker down — surfaced in the UI, build can be retried
        log.error("jobs.build_enqueue_failed", job_id=str(job_id), error=str(exc))


def _store_slides(job_id: uuid.UUID) -> list[str]:
    """Object keys of the rendered listing slides, in order (empty when none yet).

    Every index is checked rather than stopping at the first gap: slides can be
    deleted individually, and a listing that starts at 02 is still a listing. The
    old early exit hid the whole set whenever slide 01 was removed.
    """
    from iosforge.storage.client import S3ArtifactStorage, build_key

    storage = S3ArtifactStorage()
    keys: list[str] = []
    for idx in range(1, 11):  # the planner caps a listing well below this
        key = build_key(job_id=str(job_id), kind="store_assets", name=f"{idx:02d}.png")
        try:
            if storage.exists(key):
                keys.append(key)
        except Exception:
            continue
    return keys


# The pixel sizes App Store Connect accepts per screenshot slot. It validates what
# is dropped in against the slot, so a 6.9" set is refused by the 6.5" one even
# though the two are within 0.2% of the same shape.
DEVICE_SIZES: dict[str, tuple[int, int]] = {
    "iphone-69": (1290, 2796),
    "iphone-65": (1284, 2778),
    "iphone-55": (1242, 2208),
    "ipad-129": (2048, 2732),
}


def slide_for_slot(data: bytes, size: tuple[int, int] | None) -> bytes:
    """The stored slide as the chosen slot wants it: right pixels, real PNG.

    App Store Connect checks the contents against the extension, and the image model
    returns JPEG whatever the payload asks for, so bytes that skip the resize step
    still have to be transcoded before they go into the archive.
    """
    from iosforge.mvp.image_slides import to_png

    return _resize_png(data, size) if size else to_png(data)


def _resize_png(data: bytes, size: tuple[int, int]) -> bytes:
    """Re-render image bytes at ``size``; the original on failure.

    Between neighbouring iPhone slots the aspect ratio differs by a fraction of a
    percent, so scaling to fit is visually lossless and avoids regenerating a set
    that is already approved.
    """
    import subprocess as _sp

    width, height = size
    result = _sp.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-i",
            "pipe:0",
            "-vf",
            f"scale={width}:{height}:flags=lanczos",
            "-f",
            "image2",
            "-c:v",
            "png",
            "pipe:1",
        ],
        input=data,
        capture_output=True,
        check=False,
    )
    return result.stdout or data


def _slide_name(index: str) -> str | None:
    """Object name for a slide reference, or ``None`` when it is not one.

    Accepts a plain number for a phone slide and an ``ipad_NN`` form for a tablet
    one, so a single delete endpoint serves both galleries without letting an
    arbitrary string through to the storage key.
    """
    raw = index.strip().lower()
    prefix = ""
    if raw.startswith("ipad_"):
        prefix, raw = "ipad_", raw[5:]
    if not raw.isdigit():
        return None
    number = int(raw)
    if not 1 <= number <= 10:
        return None
    return f"{prefix}{number:02d}.png"


def _ipad_slides(job_id: uuid.UUID) -> list[str]:
    """Object keys of the iPad-shaped slides, in order (empty when none yet)."""
    from iosforge.storage.client import S3ArtifactStorage, build_key

    storage = S3ArtifactStorage()
    keys: list[str] = []
    for idx in range(1, 11):
        key = build_key(job_id=str(job_id), kind="store_assets", name=f"ipad_{idx:02d}.png")
        try:
            if storage.exists(key):
                keys.append(key)
        except Exception:
            continue
    return keys


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


@router.post("/jobs/{job_id}/signing")
async def jobs_signing(
    request: Request,
    job_id: uuid.UUID,
    csrf_token: str = Form(...),
    issuer_id: str = Form(""),
    key_id: str = Form(""),
    key_name: str = Form(""),
    p8: UploadFile = File(...),
    user: SessionData = Depends(require_user),
    db: Session = Depends(get_db),
) -> Response:
    """Upload THIS app's App Store Connect API key → secrets/jobs/<job_id>/ (mode 600).

    Each app ships under its own Apple account, so the signing credential is per job.
    """
    from iosforge.mvp import asc_credentials

    if not csrf.verify(user.csrf_token, csrf_token):
        return Response("Invalid request (CSRF).", status_code=400)
    if db.get(Job, job_id) is None:
        return Response("Not found", status_code=404)
    settings = get_settings()

    # Query first (the server reads it), then the fragment that opens the Build & sign tab.
    def _back(query: str) -> str:
        return f"/jobs/{job_id}?{query}#t-build"

    raw = await p8.read()
    if not raw:
        return RedirectResponse(
            _back(f"sign_error={quote_plus('Select the .p8 API key file to upload.')}"),
            status_code=HTTP_303_SEE_OTHER,
        )
    try:
        asc_credentials.store(
            settings,
            job_id=str(job_id),
            issuer_id=issuer_id,
            key_id=key_id,
            key_name=key_name,
            p8=raw,
        )
    except asc_credentials.AscCredentialsError as exc:
        return RedirectResponse(
            _back(f"sign_error={quote_plus(str(exc))}"), status_code=HTTP_303_SEE_OTHER
        )
    # never log key material
    log.info("admin.asc_credentials_saved", job_id=str(job_id), key_id_set=bool(key_id))
    return RedirectResponse(_back("signed=1"), status_code=HTTP_303_SEE_OTHER)
