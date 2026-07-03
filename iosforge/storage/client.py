"""Artifact storage abstraction over a versioned MinIO/S3 bucket (SPEC §8, §10).

The pipeline produces artifacts (screenshots, APKs, Flutter sources, reports)
that must be addressable per Job and kept with full version history. This module
exposes:

* :func:`build_key` — the canonical ``jobs/{job_id}/{kind}/{name}`` key layout
  that binds every artifact to its Job;
* :class:`ArtifactRef` / :class:`ArtifactVersion` — Pydantic descriptors of a
  stored object / one of its versions;
* :class:`ArtifactStorage` — an abstract base (Protocol-like ABC) so a second
  backend (local FS, another S3) can be added without touching callers;
* :class:`S3ArtifactStorage` — the boto3 implementation driven entirely by
  ``get_settings()`` (endpoint/keys/bucket/region), targeting MinIO locally.

Logging is structural and never includes artifact *content* — only keys, sizes
and version ids — per the "no sensitive payloads in logs" rule (SPEC §8).
"""

from __future__ import annotations

import io
from abc import ABC, abstractmethod
from collections.abc import Sequence
from typing import IO, TYPE_CHECKING

import boto3  # type: ignore[import-untyped]
from botocore.config import Config as BotoConfig  # type: ignore[import-untyped]
from botocore.exceptions import ClientError  # type: ignore[import-untyped]
from pydantic import BaseModel

from iosforge.common.config import Settings, get_settings
from iosforge.common.logging import get_logger

if TYPE_CHECKING:  # pragma: no cover - typing only
    # Optional typed-stub package; absent at runtime and in this env.
    from mypy_boto3_s3.client import S3Client  # type: ignore[import-not-found]
else:  # pragma: no cover - runtime has no stub package
    S3Client = object

_log = get_logger("storage")

# Default object content type when the caller does not specify one.
_DEFAULT_CONTENT_TYPE = "application/octet-stream"

# Reserved key-component sentinel that would let a name escape its Job prefix.
_FORBIDDEN_FRAGMENTS = ("..", "//")


def build_key(*, job_id: str, kind: str, name: str) -> str:
    """Build a Job-scoped object key ``jobs/{job_id}/{kind}/{name}`` (SPEC §10).

    ``kind`` is the artifact category (``screenshots``/``apk``/``sources``/
    ``report``/...). Each component is validated so a crafted ``name`` cannot
    traverse out of the ``jobs/{job_id}/`` prefix.

    Raises:
        ValueError: if any component is empty, absolute, or contains ``..``/``//``.
    """
    for label, component in (("job_id", job_id), ("kind", kind), ("name", name)):
        if not component:
            raise ValueError(f"{label} must be non-empty")
        if component.startswith("/"):
            raise ValueError(f"{label} must not be absolute: {component!r}")
        if any(fragment in component for fragment in _FORBIDDEN_FRAGMENTS):
            raise ValueError(f"{label} contains a forbidden path fragment: {component!r}")
    return f"jobs/{job_id}/{kind}/{name}"


def _parse_key(key: str) -> tuple[str | None, str | None]:
    """Extract ``(job_id, kind)`` from a ``jobs/{job_id}/{kind}/...`` key.

    Returns ``(None, None)`` for keys that do not match the Job-scoped layout, so
    refs can still be produced for arbitrary keys without raising.
    """
    parts = key.split("/")
    if len(parts) >= 4 and parts[0] == "jobs":
        return parts[1], parts[2]
    return None, None


class ArtifactVersion(BaseModel):
    """One version of a stored artifact (a row of ``list_versions``)."""

    version_id: str | None = None
    size: int
    is_latest: bool


class ArtifactRef(BaseModel):
    """A reference to a stored artifact object (returned by ``put``).

    Carries enough to re-fetch the exact object (bucket/key/version_id) plus the
    Job binding (``job_id``/``kind``) decoded from the key.
    """

    bucket: str
    key: str
    version_id: str | None = None
    size: int
    content_type: str
    kind: str | None = None
    job_id: str | None = None


class ArtifactStorage(ABC):
    """Backend-agnostic artifact store contract (SPEC §8, §9 swappability).

    A second implementation (local FS, another S3) only needs to subclass this
    and implement the abstract methods; orchestrator/services depend on this ABC,
    not on boto3. The full external contract is documented by T-2.4 in API_MAP.
    """

    @abstractmethod
    def put(
        self,
        key: str,
        data: bytes | IO[bytes],
        *,
        content_type: str | None = None,
    ) -> ArtifactRef:
        """Store ``data`` at ``key`` and return a ref to the written version."""

    @abstractmethod
    def get(self, key: str, *, version_id: str | None = None) -> bytes:
        """Return the bytes at ``key`` (latest, or a specific ``version_id``)."""

    @abstractmethod
    def list_versions(self, key: str) -> list[ArtifactVersion]:
        """Return all stored versions of ``key``, newest first."""

    @abstractmethod
    def exists(self, key: str) -> bool:
        """Return whether ``key`` currently resolves to a live object."""

    @abstractmethod
    def delete(self, key: str) -> None:
        """Soft-delete ``key`` (places a delete marker on a versioned bucket)."""


class S3ArtifactStorage(ArtifactStorage):
    """boto3-backed :class:`ArtifactStorage` targeting MinIO/S3 from settings.

    All connection parameters come from :func:`get_settings`; nothing is
    hardcoded. Path-style addressing is used so a single MinIO endpoint serves
    every bucket without DNS sub-domains.
    """

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self._bucket = self._settings.s3_bucket
        self._client: S3Client = boto3.client(
            "s3",
            endpoint_url=self._settings.s3_endpoint_url,
            aws_access_key_id=self._settings.s3_access_key_id or None,
            aws_secret_access_key=self._settings.s3_secret_access_key or None,
            region_name=self._settings.s3_region,
            config=BotoConfig(
                signature_version="s3v4",
                s3={"addressing_style": "path"},
                retries={"max_attempts": 3, "mode": "standard"},
            ),
        )

    @property
    def bucket(self) -> str:
        return self._bucket

    def bucket_ready(self) -> bool:
        """Return whether the configured bucket is reachable and authenticated.

        Used by tests to decide whether to run against live infra; never raises.
        """
        try:
            self._client.head_bucket(Bucket=self._bucket)
            return True
        except Exception:  # pragma: no cover - infra/creds missing
            return False

    def put(
        self,
        key: str,
        data: bytes | IO[bytes],
        *,
        content_type: str | None = None,
    ) -> ArtifactRef:
        body: IO[bytes] = io.BytesIO(data) if isinstance(data, bytes) else data
        ctype = content_type or _DEFAULT_CONTENT_TYPE
        # Measure size from the stream without trusting the caller, then rewind.
        start = body.tell()
        body.seek(0, io.SEEK_END)
        size = body.tell() - start
        body.seek(start)

        resp = self._client.put_object(
            Bucket=self._bucket,
            Key=key,
            Body=body,
            ContentType=ctype,
        )
        version_id = resp.get("VersionId")
        job_id, kind = _parse_key(key)
        _log.info(
            "artifact.put",
            bucket=self._bucket,
            key=key,
            version_id=version_id,
            size=size,
            content_type=ctype,
            job_id=job_id,
            kind=kind,
        )
        return ArtifactRef(
            bucket=self._bucket,
            key=key,
            version_id=version_id,
            size=size,
            content_type=ctype,
            kind=kind,
            job_id=job_id,
        )

    def get(self, key: str, *, version_id: str | None = None) -> bytes:
        kwargs: dict[str, str] = {"Bucket": self._bucket, "Key": key}
        if version_id is not None:
            kwargs["VersionId"] = version_id
        resp = self._client.get_object(**kwargs)
        body: bytes = resp["Body"].read()
        _log.info(
            "artifact.get",
            bucket=self._bucket,
            key=key,
            version_id=version_id,
            size=len(body),
        )
        return body

    def list_versions(self, key: str) -> list[ArtifactVersion]:
        resp = self._client.list_object_versions(Bucket=self._bucket, Prefix=key)
        versions: list[ArtifactVersion] = []
        for entry in resp.get("Versions", []):
            # ``Prefix`` is not an exact match; keep only the requested key.
            if entry.get("Key") != key:
                continue
            versions.append(
                ArtifactVersion(
                    version_id=entry.get("VersionId"),
                    size=int(entry.get("Size", 0)),
                    is_latest=bool(entry.get("IsLatest", False)),
                )
            )
        _log.info(
            "artifact.list_versions",
            bucket=self._bucket,
            key=key,
            count=len(versions),
        )
        return versions

    def exists(self, key: str) -> bool:
        try:
            self._client.head_object(Bucket=self._bucket, Key=key)
            return True
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code")
            # 404 (missing) and 405 (delete marker is current) -> not live.
            if code in ("404", "405", "NoSuchKey"):
                return False
            raise

    def delete(self, key: str) -> None:
        # On a versioned bucket this writes a delete marker; prior versions stay.
        self._client.delete_object(Bucket=self._bucket, Key=key)
        _log.info("artifact.delete", bucket=self._bucket, key=key)

    def purge(self, key: str) -> None:
        """Hard-delete every version of ``key`` (test cleanup / explicit GC).

        This permanently removes history and is *not* part of the abstract
        contract — callers in the pipeline use :meth:`delete` (soft).
        """
        version_ids: Sequence[str | None] = [v.version_id for v in self.list_versions(key)]
        for vid in version_ids:
            kwargs: dict[str, str] = {"Bucket": self._bucket, "Key": key}
            if vid is not None:
                kwargs["VersionId"] = vid
            self._client.delete_object(**kwargs)
        # Also clear any delete markers so the key is fully gone.
        resp = self._client.list_object_versions(Bucket=self._bucket, Prefix=key)
        for marker in resp.get("DeleteMarkers", []):
            if marker.get("Key") != key:
                continue
            self._client.delete_object(
                Bucket=self._bucket,
                Key=key,
                VersionId=marker["VersionId"],
            )
