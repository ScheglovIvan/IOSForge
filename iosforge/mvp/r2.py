"""Cloudflare R2 media host (S3-compatible) — placeholder media for generated apps.

R2 replaces Firebase Storage on the no-Blaze path: one shared bucket
(``settings.r2_bucket``) with a per-app key prefix, served publicly from
``settings.r2_public_base``. Credentials (endpoint + access key + secret) are read
from the gitignored env file at ``settings.r2_secrets_path`` (falling back to the
process environment), never committed.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

import boto3  # type: ignore[import-untyped]

from iosforge.common.config import Settings
from iosforge.common.logging import get_logger

if TYPE_CHECKING:
    from mypy_boto3_s3.client import S3Client  # type: ignore[import-not-found]

log = get_logger("mvp.r2")

_REQUIRED = ("R2_ENDPOINT", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY")


class R2ConfigError(RuntimeError):
    """R2 is requested but bucket/credentials are missing or incomplete."""


def load_credentials(settings: Settings) -> dict[str, str]:
    """Return {endpoint, access_key_id, secret} from the secrets file or the env."""
    values: dict[str, str] = {}
    path = settings.r2_secrets_path
    if path and Path(path).is_file():
        for line in Path(path).read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, val = line.split("=", 1)
            values[key.strip()] = val.strip().strip('"').strip("'")
    creds = {key: values.get(key) or os.environ.get(key, "") for key in _REQUIRED}
    missing = [key for key, val in creds.items() if not val]
    if missing:
        raise R2ConfigError(f"R2 credentials incomplete (missing {missing})")
    return {
        "endpoint": creds["R2_ENDPOINT"],
        "access_key_id": creds["R2_ACCESS_KEY_ID"],
        "secret": creds["R2_SECRET_ACCESS_KEY"],
    }


def client(settings: Settings) -> S3Client:
    """Build a boto3 S3 client bound to the R2 endpoint."""
    creds = load_credentials(settings)
    return boto3.client(
        "s3",
        endpoint_url=creds["endpoint"],
        aws_access_key_id=creds["access_key_id"],
        aws_secret_access_key=creds["secret"],
        region_name="auto",
    )


def public_url(settings: Settings, key: str) -> str:
    """Map an object key to its public r2.dev (or custom-domain) URL."""
    base = settings.r2_public_base.rstrip("/")
    return f"{base}/{key.lstrip('/')}"


def upload_file(s3: S3Client, settings: Settings, local: Path, key: str, content_type: str) -> str:
    """Upload ``local`` to ``settings.r2_bucket`` under ``key``; return its public URL."""
    if not settings.r2_bucket:
        raise R2ConfigError("settings.r2_bucket is empty")
    extra: dict[str, Any] = {"ContentType": content_type}
    s3.upload_file(str(local), settings.r2_bucket, key, ExtraArgs=extra)
    return public_url(settings, key)
