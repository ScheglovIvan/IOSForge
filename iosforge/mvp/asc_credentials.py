"""App Store Connect signing credential, uploaded PER JOB in the admin and kept in secrets/.

Each app ships under its own Apple account, so the operator uploads that app's App Store
Connect API key (.p8 + Issuer ID + Key ID) on the job's own detail page. Instead of
registering it inside the CodeMagic UI (the manual steps 1-6 of the залив-through-CodeMagic
runbook), the pipeline injects it into the signed build as environment variables at trigger
time, so CodeMagic's ``app-store-connect`` CLI fetches/creates the distribution certificate +
provisioning profile on the fly and the ``publishing`` step uploads the .ipa. A reusable RSA
certificate private key is generated once per app so re-running a build re-uses the same
signing certificate (Apple caps distribution certificates per account) rather than minting a
new one each time.

Everything lives in gitignored files under ``secrets/jobs/<job_id>/`` with mode 600 — never
the database, never ``.env``, never git. A credential dropped in the shared ``settings``
paths (no UI) is an optional fallback for jobs that have none of their own. The public status
view never exposes the private key material.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from iosforge.common.config import Settings
from iosforge.common.logging import get_logger

log = get_logger("mvp.asc_credentials")

_ISSUER_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
_KEY_ID_RE = re.compile(r"^[A-Za-z0-9]{8,12}$")
_KEY_NAME_RE = re.compile(r"^[A-Za-z0-9 _\-]{1,64}$")


class AscCredentialsError(ValueError):
    """An uploaded App Store Connect credential failed validation."""


@dataclass(frozen=True)
class AscCredentials:
    """A validated, on-disk App Store Connect signing credential."""

    issuer_id: str
    key_id: str
    key_name: str
    private_key_p8: str
    certificate_private_key: str


_JOB_ID_RE = re.compile(r"^[A-Za-z0-9._\-]{1,64}$")


def _job_dir(settings: Settings, job_id: str) -> Path:
    if not _JOB_ID_RE.match(job_id):
        raise AscCredentialsError("Invalid job id.")
    return Path(settings.asc_jobs_secrets_dir) / job_id


def _env_path(settings: Settings, job_id: str | None) -> Path:
    if job_id is None:
        return Path(settings.asc_api_key_secrets_path)
    return _job_dir(settings, job_id) / "asc_api_key.env"


def _p8_path(settings: Settings, job_id: str | None) -> Path:
    if job_id is None:
        return Path(settings.asc_api_key_p8_path)
    return _job_dir(settings, job_id) / "asc_api_key.p8"


def p8_path(settings: Settings, job_id: str | None) -> Path:
    """Where the job's ``.p8`` API key is stored (the global key path without a job)."""
    return _p8_path(settings, job_id)


def _cert_key_path(settings: Settings, job_id: str | None) -> Path:
    if job_id is None:
        return Path(settings.asc_certificate_key_path)
    return _job_dir(settings, job_id) / "asc_certificate_private_key.pem"


def _write_secret(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(0o600)


def _generate_certificate_key() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return pem.decode()


def _ensure_certificate_key(settings: Settings, job_id: str | None) -> str:
    path = _cert_key_path(settings, job_id)
    if path.is_file():
        existing = path.read_text().strip()
        if existing:
            return existing
    pem = _generate_certificate_key()
    _write_secret(path, pem)
    log.info("asc_credentials.certificate_key_generated", job_id=job_id or "")
    return pem


def store(
    settings: Settings,
    *,
    job_id: str,
    issuer_id: str,
    key_id: str,
    key_name: str,
    p8: bytes,
) -> None:
    """Validate and persist a job's uploaded App Store Connect API key under secrets/jobs/.

    Writes the metadata env file + the .p8 (mode 600) and, on first upload, generates the
    reusable certificate private key. Raises ``AscCredentialsError`` on invalid input.
    """
    issuer_id = (issuer_id or "").strip()
    key_id = (key_id or "").strip()
    key_name = (key_name or "").strip()
    if not _ISSUER_RE.match(issuer_id):
        raise AscCredentialsError("Issuer ID must be a UUID (from ASC → Users and Access).")
    if not _KEY_ID_RE.match(key_id):
        raise AscCredentialsError("Key ID must be the short alphanumeric API key identifier.")
    if key_name and not _KEY_NAME_RE.match(key_name):
        raise AscCredentialsError("Key name may contain letters, digits, spaces, - and _ only.")
    if len(p8) > settings.asc_api_key_max_bytes:
        raise AscCredentialsError("The .p8 file is larger than expected — is it the right file?")
    try:
        text = p8.decode("ascii")
    except UnicodeDecodeError as exc:
        raise AscCredentialsError("The .p8 file is not a text PEM key.") from exc
    if "PRIVATE KEY" not in text:
        raise AscCredentialsError("The uploaded file is not a .p8 private key (no PEM header).")

    _write_secret(_p8_path(settings, job_id), text.strip() + "\n")
    _write_secret(
        _env_path(settings, job_id),
        f"APP_STORE_CONNECT_ISSUER_ID={issuer_id}\n"
        f"APP_STORE_CONNECT_KEY_IDENTIFIER={key_id}\n"
        f"ASC_KEY_NAME={key_name}\n",
    )
    _ensure_certificate_key(settings, job_id)
    log.info("asc_credentials.stored", job_id=job_id, key_id=key_id, key_name_set=bool(key_name))


def _read_env(settings: Settings, job_id: str | None) -> dict[str, str]:
    path = _env_path(settings, job_id)
    if not path.is_file():
        return {}
    out: dict[str, str] = {}
    for line in path.read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            name, _, value = line.partition("=")
            out[name.strip()] = value.strip()
    return out


def _load_scope(settings: Settings, job_id: str | None) -> AscCredentials | None:
    env = _read_env(settings, job_id)
    issuer = env.get("APP_STORE_CONNECT_ISSUER_ID", "")
    key_id = env.get("APP_STORE_CONNECT_KEY_IDENTIFIER", "")
    p8_path = _p8_path(settings, job_id)
    if not (issuer and key_id and p8_path.is_file()):
        return None
    p8 = p8_path.read_text().strip()
    if not p8:
        return None
    return AscCredentials(
        issuer_id=issuer,
        key_id=key_id,
        key_name=env.get("ASC_KEY_NAME", ""),
        private_key_p8=p8,
        certificate_private_key=_ensure_certificate_key(settings, job_id),
    )


def load(settings: Settings, job_id: str) -> AscCredentials | None:
    """The job's own credential, or the shared fallback, or None when neither is present."""
    return _load_scope(settings, job_id) or _load_scope(settings, None)


def is_configured(settings: Settings, job_id: str) -> bool:
    """Whether a usable signing credential is on disk for this job (own or shared)."""
    return load(settings, job_id) is not None


def status(settings: Settings, job_id: str) -> dict[str, object]:
    """A UI-safe summary — never exposes private key material."""
    own = _load_scope(settings, job_id)
    creds = own or _load_scope(settings, None)
    if creds is None:
        return {"configured": False, "own": False, "key_name": "", "key_id": "", "issuer_id": ""}
    issuer = creds.issuer_id
    masked_issuer = f"{issuer[:8]}…{issuer[-4:]}" if len(issuer) > 12 else issuer
    return {
        "configured": True,
        "own": own is not None,
        "key_name": creds.key_name,
        "key_id": creds.key_id,
        "issuer_id": masked_issuer,
    }
