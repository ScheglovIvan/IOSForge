"""Smoke tests for the admin panel — pure logic + auth guard (no live infra).

Covers the security-critical path: APK validation, password hashing, and that
unauthenticated requests never reach data (redirect to login). The full upload /
worker flow is exercised by a live run, not here.
"""

from __future__ import annotations

import io
import zipfile

import pytest
from fastapi.testclient import TestClient

from iosforge.admin import csrf
from iosforge.admin.apk_validate import ApkValidationError, sanitize_filename, validate
from iosforge.admin.app import create_app
from iosforge.admin.security import hash_password, verify_password


def _fake_apk() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("AndroidManifest.xml", b"\x00fake")
        zf.writestr("classes.dex", b"\x00")
    return buf.getvalue()


def test_validate_accepts_real_apk() -> None:
    v = validate("MyApp.apk", _fake_apk(), max_bytes=10_000_000)
    assert v.filename == "MyApp.apk"
    assert v.size > 0 and len(v.sha256) == 64


def test_validate_rejects_non_apk_and_bad_zip() -> None:
    with pytest.raises(ApkValidationError):
        validate("evil.exe", _fake_apk(), max_bytes=10_000_000)
    with pytest.raises(ApkValidationError):
        validate("notzip.apk", b"not a zip", max_bytes=10_000_000)
    with pytest.raises(ApkValidationError):  # zip without AndroidManifest.xml
        validate("plain.apk", _plain_zip(), max_bytes=10_000_000)
    with pytest.raises(ApkValidationError):  # over size limit
        validate("big.apk", _fake_apk(), max_bytes=10)


def _plain_zip() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("hello.txt", b"hi")
    return buf.getvalue()


def _fake_xapk() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("com.example.base.apk", _fake_apk())
        zf.writestr("config.arm64_v8a.apk", b"\x00split")
        zf.writestr("manifest.json", b'{"package_name":"com.example"}')
    return buf.getvalue()


def test_validate_accepts_xapk_bundle() -> None:
    v = validate("Sticker_Album.xapk", _fake_xapk(), max_bytes=10_000_000)
    assert v.filename == "Sticker_Album.xapk"


def test_validate_rejects_xapk_without_apk_parts() -> None:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("manifest.json", b"{}")
    with pytest.raises(ApkValidationError):
        validate("empty.xapk", buf.getvalue(), max_bytes=10_000_000)


def test_sanitize_filename_strips_paths() -> None:
    assert sanitize_filename("../../etc/passwd") == "passwd.apk"
    assert sanitize_filename("a b/c;rm -rf.apk").endswith(".apk")
    assert "/" not in sanitize_filename("x/y/z.apk")


def test_password_hash_roundtrip() -> None:
    h = hash_password("Sup3rSecret123!")
    assert h != "Sup3rSecret123!"  # never plaintext
    assert verify_password(h, "Sup3rSecret123!")
    assert not verify_password(h, "wrong")


def test_csrf_verify_constant_time_logic() -> None:
    t = csrf.new_token()
    assert csrf.verify(t, t)
    assert not csrf.verify(t, "other")
    assert not csrf.verify(None, t)
    assert not csrf.verify(t, "")


def test_unauthenticated_request_redirects_to_login() -> None:
    client = TestClient(create_app())
    # Browser navigation -> redirect to /login (never serves the data page).
    r = client.get("/jobs", headers={"accept": "text/html"}, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/login"
    # Non-browser client -> 401, not data.
    r2 = client.get("/jobs", headers={"accept": "application/json"}, follow_redirects=False)
    assert r2.status_code == 401
    # Health stays open.
    assert client.get("/health").status_code == 200
