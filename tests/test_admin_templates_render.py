"""The redesigned admin templates render across their branches (no live infra)."""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace as NS

from iosforge.admin.templating import templates

_ENV = templates.env
_REQ = NS(url=NS(path="/jobs"))
_USER = NS(username="admin", csrf_token="csrf-tok")
_NOW = datetime(2026, 7, 21, 9, 30, 0)


def _render(name: str, **ctx: object) -> str:
    ctx.setdefault("request", _REQ)
    return _ENV.get_template(name).render(**ctx)


def test_login_renders_anon_and_error() -> None:
    html = _render("login.html", user=None, csrf_token="t", error="Invalid username or password.")
    assert "/static/app.css" in html
    assert "Invalid username" in html
    assert 'name="csrf_token"' in html


def test_jobs_list_renders_with_and_without_jobs() -> None:
    job = NS(
        id="a05c37ae-1111-2222-3333-444455556666",
        source_app_ref="CarPlay",
        state=NS(value="codegen"),
        created_at=_NOW,
    )
    html = _render("jobs_list.html", user=_USER, jobs=[job])
    assert "CarPlay" in html and "pill" in html  # status pill rendered
    empty = _render("jobs_list.html", user=_USER, jobs=[])
    assert "No jobs yet" in empty


def test_job_detail_in_progress_without_gen() -> None:
    job = NS(
        id="job-1",
        source_app_ref="CarPlay",
        state=NS(value="codegen"),
        created_at=_NOW,
        source_app_metadata={},
        result_version=1,
    )
    signing = {"configured": False, "own": False, "key_name": "", "key_id": "", "issuer_id": ""}
    html = _render(
        "job_detail.html",
        user=_USER,
        job=job,
        gen=None,
        build=None,
        can_ios_build=False,
        store_slides=[],
        signing=signing,
        signed_ok=False,
        sign_error="",
        timeline=[],
        codegen_tasks=[],
    )
    # laid out as logical tabs, not one vertical stack
    assert 'class="tab-bar"' in html
    assert 'id="t-overview"' in html and 'id="t-build"' in html
    assert "panel-overview" in html and "panel-build" in html
    # core forms are preserved even before generation (build + signing live in the Build tab)
    assert 'action="/jobs/job-1/build-settings"' in html
    assert 'action="/jobs/job-1/signing"' in html
    assert 'name="p8"' in html and 'name="appstore_apple_id"' in html


def test_job_detail_done_full_state() -> None:
    job = NS(
        id="job-2",
        source_app_ref="Speaker Cleaner",
        state=NS(value="done"),
        created_at=_NOW,
        result_version=3,
        source_app_metadata={
            "build_profile": "store",
            "override_app_name": "Speaker Cleaner",
            "override_bundle_id": "com.acme.sc",
            "appstore_apple_id": "6480123456",
            "apphud_api_key": "appstr_x",
            "tenjin_api_key": "ABC123",
        },
    )
    gen = NS(
        github_repo_url="https://github.com/x/y",
        codemagic={"application_id": "cm1", "project_name": "P", "default_branch": "main"},
        selftest_report={
            "structural": {
                "ok": True,
                "missing_screens": [],
                "blank_screens": [],
                "dead_links": [],
                "missing_edges": [],
            }
        },
        sources_key="k",
        compliance_score=0.97,
    )
    build = NS(
        status="finished",
        build_number=5,
        build_id="b1",
        started_at=_NOW,
        finished_at=_NOW,
        duration_ms=123000,
        message="",
        artifacts=[{"name": "app.ipa", "size": 2048}],
    )
    signing = {
        "configured": True,
        "own": True,
        "key_name": "SCKey",
        "key_id": "2X9R4HXF34",
        "issuer_id": "69a6de70…a4d1",
    }
    timeline = [
        NS(stage=NS(value="codegen"), started_at=_NOW, finished_at=_NOW, duration_ms=42, error=None)
    ]
    codegen_tasks = [
        NS(idx=0, total=2, title="Home", status="done", attempts=1),
        NS(idx=1, total=2, title="Paywall", status="running", attempts=1),
    ]
    html = _render(
        "job_detail.html",
        user=_USER,
        job=job,
        gen=gen,
        build=build,
        can_ios_build=True,
        store_slides=["jobs/x/store_assets/01.png", "jobs/x/store_assets/02.png"],
        signing=signing,
        signed_ok=True,
        sign_error="",
        timeline=timeline,
        codegen_tasks=codegen_tasks,
    )
    assert 'action="/jobs/job-2/codemagic-build"' in html  # iOS build card present
    assert 'action="/jobs/job-2/store-assets/slide"' in html  # per-slide refine
    assert "2X9R4HXF34" in html and "Speaker Cleaner" in html
    # all logical tabs present once the app is generated
    for tab in ("t-overview", "t-build", "t-ios", "t-store", "t-rework"):
        assert f'id="{tab}"' in html
    assert "panel-store" in html and "panel-rework" in html


def test_job_artifacts_empty_state() -> None:
    job = NS(id="job-3")
    html = _render(
        "job_artifacts.html",
        user=_USER,
        job=job,
        walk=NS(screenshot_keys=[], screen_map=None),
        gen=None,
    )
    assert "No screenshots yet" in html and "No screen map yet" in html


def test_icon_gallery_excludes_the_current_one() -> None:
    # the current icon is already displayed large above the gallery; repeating it in
    # the list of alternatives showed the same picture twice
    from jinja2 import Environment

    rows = [
        {"version_id": "v3", "is_latest": True, "at": "09:30"},
        {"version_id": "v2", "is_latest": False, "at": "09:25"},
        {"version_id": "v1", "is_latest": False, "at": "08:43"},
    ]
    tpl = Environment().from_string(
        "{% set e = icon_versions | rejectattr('is_latest') | list %}"
        "{{ e|length }}|{{ e|map(attribute='version_id')|join(',') }}"
    )
    assert tpl.render(icon_versions=rows) == "2|v2,v1"


def test_icon_gallery_is_hidden_when_there_is_only_the_current_one() -> None:
    from jinja2 import Environment

    tpl = Environment().from_string(
        "{% set e = icon_versions | rejectattr('is_latest') | list %}"
        "{{ 'shown' if e else 'hidden' }}"
    )
    assert tpl.render(icon_versions=[{"version_id": "v1", "is_latest": True}]) == "hidden"


def test_job_detail_native_build_card() -> None:
    job = NS(
        id="job-3", state=NS(value="done"), source_app_metadata={}, candidate=None,
        created_at=_NOW, updated_at=_NOW, error=None,
    )  # fmt: skip
    xbuild = NS(
        status="ready_for_upload", signed=True, version="1.0", build_number="2610091200",
        duration_ms=61000, message=None, ipa_key="jobs/job-3/ipa/Demo.ipa",
    )  # fmt: skip
    html = _render(
        "job_detail.html", user=_USER, job=job, gen=None, build=None, can_ios_build=True,
        can_codemagic_build=False, can_xcode_delivery=True, xcode_build=xbuild,
        signing={"configured": False}, signed_ok=False, sign_error="", timeline=[],
        codegen_tasks=[],
    )  # fmt: skip
    assert 'action="/jobs/job-3/xcode-delivery"' in html
    assert "/jobs/job-3/xcode-delivery/ipa" in html and "2610091200" in html
    assert "Uploading to App Store Connect is disabled" in html
    assert 'action="/jobs/job-3/codemagic-build"' not in html
