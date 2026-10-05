"""Per-app App Store signing key upload lives on the job detail page (not global settings)."""

from __future__ import annotations

from pathlib import Path

import jinja2

_ROOT = Path(__file__).resolve().parents[1]
_TEMPLATES = _ROOT / "iosforge" / "admin" / "templates"
_JOB_DETAIL = _TEMPLATES / "job_detail.html"
_BASE = _TEMPLATES / "base.html"
_ROUTES = _ROOT / "iosforge" / "admin" / "routes_jobs.py"


def test_job_detail_template_is_valid_jinja() -> None:
    env = jinja2.Environment(autoescape=True, loader=jinja2.FileSystemLoader(str(_TEMPLATES)))
    env.get_template("job_detail.html")


def test_signing_card_is_on_the_job_page_with_upload_fields() -> None:
    html = _JOB_DETAIL.read_text()
    assert 'id="signing"' in html
    assert 'action="/jobs/{{ job.id }}/signing"' in html
    assert 'enctype="multipart/form-data"' in html
    assert 'name="p8"' in html and 'type="file"' in html
    for field in ("issuer_id", "key_id", "key_name", "csrf_token"):
        assert f'name="{field}"' in html


def test_per_job_route_is_csrf_protected_and_scoped() -> None:
    routes = _ROUTES.read_text()
    assert '@router.post("/jobs/{job_id}/signing")' in routes
    assert "csrf.verify(user.csrf_token, csrf_token)" in routes
    # the credential is stored under the job's own scope
    assert "asc_credentials.store(" in routes
    assert "job_id=str(job_id)" in routes


def test_no_global_signing_settings_page_remains() -> None:
    assert not (_TEMPLATES / "settings_signing.html").exists()
    assert "/settings/signing" not in _ROUTES.read_text()
    assert "/settings/signing" not in _BASE.read_text()
