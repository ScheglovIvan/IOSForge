"""Per-job Apphud SDK key in the admin build-settings form.

Apphud issues one publishable key per app, so the key is stored on the job
(``source_app_metadata``) rather than only in the shared secrets file.
"""

from __future__ import annotations

import re
from pathlib import Path

import jinja2

_ROOT = Path(__file__).resolve().parents[1]
_TEMPLATE = _ROOT / "iosforge" / "admin" / "templates" / "job_detail.html"
_ROUTES = _ROOT / "iosforge" / "admin" / "routes_jobs.py"
_RUN_JOB = _ROOT / "iosforge" / "worker" / "run_job.py"

# the strings that must match on both sides of the DB round-trip
_META_KEY = "apphud_api_key"
_TENJIN_KEY = "tenjin_api_key"


def test_job_detail_template_is_valid_jinja() -> None:
    jinja2.Environment(autoescape=True).parse(_TEMPLATE.read_text())


def test_template_exposes_apphud_key_field() -> None:
    html = _TEMPLATE.read_text()
    assert f'name="{_META_KEY}"' in html
    # prefilled from the job's metadata so the operator sees which key is set
    assert f"meta.get('{_META_KEY}', '')" in html


def test_template_has_no_stale_revenuecat_copy() -> None:
    # the pipeline migrated to Apphud — leftover copy would mislead the operator
    assert "RevenueCat" not in _TEMPLATE.read_text()


def test_template_exposes_tenjin_key_field() -> None:
    html = _TEMPLATE.read_text()
    assert f'name="{_TENJIN_KEY}"' in html
    assert f"meta.get('{_TENJIN_KEY}', '')" in html


def test_admin_and_worker_agree_on_metadata_keys() -> None:
    # a mismatch here silently drops the per-job key and the app ships the wrong one
    routes, run_job = _ROUTES.read_text(), _RUN_JOB.read_text()
    for key in (_META_KEY, _TENJIN_KEY):
        assert f'meta["{key}"]' in routes
        assert f'.get("{key}")' in run_job


def test_tenjin_key_validation_pattern() -> None:
    pattern = r"[A-Za-z0-9]{16,64}"
    assert re.fullmatch(pattern, "ABCDEFGH1JKLMNOPQRSTUVWXYZ234567")  # Tenjin key shape
    assert not re.fullmatch(pattern, "short")
    assert not re.fullmatch(pattern, "KEY WITH SPACE")


def test_key_validation_pattern_accepts_real_apphud_keys() -> None:
    pattern = r"[A-Za-z0-9_\-]{8,128}"
    assert re.fullmatch(pattern, "appstr_AbCdEfGhIjKlMnOpQrStUvWxYz0123456789")
    assert re.fullmatch(pattern, "app_1234567890abcdef")
    # rejected: whitespace / quotes / injection-ish payloads
    assert not re.fullmatch(pattern, "appstr_abc def")
    assert not re.fullmatch(pattern, "short")
    assert not re.fullmatch(pattern, "appstr_<script>")
