"""Tests for the per-job App Store Connect signing credential (secrets/jobs/<id>/)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from iosforge.common.config import Settings
from iosforge.mvp import asc_credentials as ac

_P8 = "-----BEGIN PRIVATE KEY-----\nMIGTAgEAMBMGByqGSM49\n-----END PRIVATE KEY-----\n"
_ISSUER = "69a6de70-0000-47e3-e053-5b8c7c11a4d1"
_KEY_ID = "2X9R4HXF34"
_JOB = "a05c37ae-fb15-43ad-9606-ba986fd33201"
_JOB2 = "ae66f4ea-4d71-44f4-bc47-14fde3cfd22b"


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        codemagic_bundle_prefix="com.batteam",
        asc_jobs_secrets_dir=str(tmp_path / "jobs"),
        asc_api_key_secrets_path=str(tmp_path / "shared.env"),
        asc_api_key_p8_path=str(tmp_path / "shared.p8"),
        asc_certificate_key_path=str(tmp_path / "shared_cert.pem"),
    )


def _store(s: Settings, job: str, key_id: str = _KEY_ID, name: str = "k") -> None:
    ac.store(s, job_id=job, issuer_id=_ISSUER, key_id=key_id, key_name=name, p8=_P8.encode())


def test_not_configured_before_upload(tmp_path: Path) -> None:
    s = _settings(tmp_path)
    assert ac.is_configured(s, _JOB) is False
    assert ac.load(s, _JOB) is None
    assert ac.build_env(s, _JOB) is None
    assert ac.status(s, _JOB)["configured"] is False


def test_store_then_load_roundtrip(tmp_path: Path) -> None:
    s = _settings(tmp_path)
    _store(s, _JOB, name="EnhancerApiKey")
    creds = ac.load(s, _JOB)
    assert creds is not None
    assert creds.issuer_id == _ISSUER
    assert creds.key_id == _KEY_ID
    assert creds.key_name == "EnhancerApiKey"
    assert "RSA PRIVATE KEY" in creds.certificate_private_key


def test_credential_is_scoped_per_job(tmp_path: Path) -> None:
    s = _settings(tmp_path)
    _store(s, _JOB, key_id="AAAA111111")
    # a different job with no key of its own and no shared fallback sees nothing
    assert ac.is_configured(s, _JOB2) is False
    got = ac.load(s, _JOB)
    assert got is not None and got.key_id == "AAAA111111"


def test_shared_fallback_when_job_has_no_own_key(tmp_path: Path) -> None:
    s = _settings(tmp_path)
    _store(s, None, key_id="SHARED0001")  # dropped in the shared paths (no UI)
    st = ac.status(s, _JOB2)
    assert st["configured"] is True
    assert st["own"] is False  # using the shared fallback
    # the job's own key takes precedence over the shared one
    _store(s, _JOB2, key_id="OWNKEY0002")
    st2 = ac.status(s, _JOB2)
    assert st2["own"] is True and st2["key_id"] == "OWNKEY0002"


def test_build_env_has_the_four_signing_vars(tmp_path: Path) -> None:
    s = _settings(tmp_path)
    _store(s, _JOB, name="")
    env = ac.build_env(s, _JOB)
    assert env is not None
    assert env["APP_STORE_CONNECT_ISSUER_ID"] == _ISSUER
    assert env["APP_STORE_CONNECT_KEY_IDENTIFIER"] == _KEY_ID
    assert "PRIVATE KEY" in env["APP_STORE_CONNECT_PRIVATE_KEY"]
    assert "RSA PRIVATE KEY" in env["CERTIFICATE_PRIVATE_KEY"]


def test_secret_files_are_mode_600(tmp_path: Path) -> None:
    s = _settings(tmp_path)
    _store(s, _JOB)
    job_dir = Path(s.asc_jobs_secrets_dir) / _JOB
    for name in ("asc_api_key.p8", "asc_api_key.env", "asc_certificate_private_key.pem"):
        p = job_dir / name
        assert oct(os.stat(p).st_mode & 0o777) == "0o600", p


def test_certificate_key_is_reused_not_regenerated(tmp_path: Path) -> None:
    s = _settings(tmp_path)
    _store(s, _JOB)
    first = ac.build_env(s, _JOB)
    _store(s, _JOB, name="changed")
    second = ac.build_env(s, _JOB)
    assert first is not None and second is not None
    assert second["CERTIFICATE_PRIVATE_KEY"] == first["CERTIFICATE_PRIVATE_KEY"]


def test_status_masks_issuer_and_hides_key_material(tmp_path: Path) -> None:
    s = _settings(tmp_path)
    _store(s, _JOB, name="EnhancerApiKey")
    st = ac.status(s, _JOB)
    assert st["configured"] is True and st["key_id"] == _KEY_ID
    assert st["issuer_id"] != _ISSUER and "…" in str(st["issuer_id"])
    assert "PRIVATE KEY" not in str(st)


@pytest.mark.parametrize(
    ("issuer", "key_id", "p8", "needle"),
    [
        ("not-a-uuid", _KEY_ID, _P8.encode(), "Issuer ID"),
        (_ISSUER, "bad id!", _P8.encode(), "Key ID"),
        (_ISSUER, _KEY_ID, b"just some bytes, no pem header", "private key"),
        (_ISSUER, _KEY_ID, b"x" * 20000, "larger than expected"),
    ],
)
def test_store_rejects_invalid_input(
    tmp_path: Path, issuer: str, key_id: str, p8: bytes, needle: str
) -> None:
    s = _settings(tmp_path)
    with pytest.raises(ac.AscCredentialsError) as exc:
        ac.store(s, job_id=_JOB, issuer_id=issuer, key_id=key_id, key_name="", p8=p8)
    assert needle.lower() in str(exc.value).lower()
    assert ac.is_configured(s, _JOB) is False


def test_store_rejects_bad_job_id(tmp_path: Path) -> None:
    s = _settings(tmp_path)
    with pytest.raises(ac.AscCredentialsError):
        ac.store(
            s, job_id="../escape", issuer_id=_ISSUER, key_id=_KEY_ID, key_name="", p8=_P8.encode()
        )
