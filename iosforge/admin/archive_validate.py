"""Data-archive upload validation (SPEC §8 — untrusted file handling).

Validates the Frida-collected data archive uploaded alongside the App Store URL:
checks the extension, enforces the size limit, verifies the bytes look like a
real archive container (zip / gzip / tar) and sanitizes the filename. The archive
is stored untouched in MinIO (outside any web root) and is never executed here.
Any future stage that *unpacks* it MUST guard against zip-slip and decompression
bombs — this module only accepts the upload, it does not open the archive.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]")

_EXT_CONTAINER = {
    ".tar.gz": "tar.gz",
    ".tgz": "tar.gz",
    ".zip": "zip",
    ".tar": "tar",
}
# Longest suffix first so ".tar.gz" wins over ".tar".
_ALLOWED_EXT = tuple(sorted(_EXT_CONTAINER, key=len, reverse=True))

_ZIP_MAGICS = (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")
_GZIP_MAGIC = b"\x1f\x8b"
_USTAR_MAGIC = b"ustar"
_TAR_BLOCK = 512


class ArchiveValidationError(Exception):
    """Raised when an uploaded file is not an acceptable data archive."""


@dataclass
class ValidatedArchive:
    filename: str
    container: str
    data: bytes
    sha256: str
    size: int


def _match_ext(lower: str) -> str | None:
    return next((e for e in _ALLOWED_EXT if lower.endswith(e)), None)


def sanitize_filename(raw: str) -> str:
    """Strip any path and reduce to a safe basename ending in an allowed ext."""
    base = raw.replace("\\", "/").split("/")[-1].strip()
    lower = base.lower()
    ext = _match_ext(lower)
    base = _SAFE_NAME.sub("_", base) or "archive.zip"
    if ext is None or not base.lower().endswith(tuple(_EXT_CONTAINER)):
        base += ".zip"
    return base[:255]


def _looks_like_archive(data: bytes, container: str) -> bool:
    if container == "zip":
        return data[:4] in _ZIP_MAGICS
    if container == "tar.gz":
        return data[:2] == _GZIP_MAGIC
    if container == "tar":
        if len(data) >= _TAR_BLOCK and data[257:262] == _USTAR_MAGIC:
            return True
        # Legacy v7 tar has no magic; accept only a positive 512-block-aligned size.
        return len(data) >= _TAR_BLOCK and len(data) % _TAR_BLOCK == 0
    return False


def validate(raw_name: str, data: bytes, max_bytes: int) -> ValidatedArchive:
    """Validate uploaded bytes as a data archive, or raise."""
    ext = _match_ext(raw_name.lower())
    if ext is None:
        raise ArchiveValidationError("file must be an archive (.zip, .tar.gz, .tgz, .tar)")
    size = len(data)
    if size == 0:
        raise ArchiveValidationError("file is empty")
    if size > max_bytes:
        raise ArchiveValidationError(f"file exceeds the {max_bytes} byte limit")
    container = _EXT_CONTAINER[ext]
    if not _looks_like_archive(data, container):
        raise ArchiveValidationError("file is not a valid archive container")
    return ValidatedArchive(
        filename=sanitize_filename(raw_name),
        container=container,
        data=data,
        sha256=hashlib.sha256(data).hexdigest(),
        size=size,
    )
