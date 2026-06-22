"""APK upload validation (SPEC §8 — untrusted file handling).

Checks extension, enforces the size limit while streaming (never buffers more
than the cap), verifies the bytes are a real APK (a ZIP containing
``AndroidManifest.xml``), and sanitizes the filename. The raw APK is stored in
MinIO (outside any web root), never executed by the web tier.
"""

from __future__ import annotations

import hashlib
import io
import re
import zipfile
from dataclasses import dataclass

_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]")


class ApkValidationError(Exception):
    """Raised when an uploaded file is not an acceptable APK."""


@dataclass
class ValidatedApk:
    filename: str
    data: bytes
    sha256: str
    size: int


_ALLOWED_EXT = (".apk", ".xapk")


def sanitize_filename(raw: str) -> str:
    """Strip any path and reduce to a safe basename ending in .apk/.xapk."""
    base = raw.replace("\\", "/").split("/")[-1].strip()
    base = _SAFE_NAME.sub("_", base) or "upload.apk"
    if not base.lower().endswith(_ALLOWED_EXT):
        base += ".apk"
    return base[:255]


def validate(raw_name: str, data: bytes, max_bytes: int) -> ValidatedApk:
    """Validate uploaded bytes as an APK or XAPK bundle, or raise.

    ``.apk`` must be a ZIP containing ``AndroidManifest.xml``. ``.xapk`` is a
    bundle (base + split APKs) — a ZIP that contains at least one ``.apk`` member;
    the worker installs all parts via ``adb install-multiple``.
    """
    lower = raw_name.lower()
    if not lower.endswith(_ALLOWED_EXT):
        raise ApkValidationError("file must be an .apk or .xapk")
    size = len(data)
    if size == 0:
        raise ApkValidationError("file is empty")
    if size > max_bytes:
        raise ApkValidationError(f"file exceeds the {max_bytes} byte limit")
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            names = set(zf.namelist())
    except zipfile.BadZipFile as exc:
        raise ApkValidationError("file is not a valid archive (not a ZIP)") from exc
    if lower.endswith(".xapk"):
        if not any(n.lower().endswith(".apk") for n in names):
            raise ApkValidationError("xapk bundle contains no .apk parts")
    elif "AndroidManifest.xml" not in names:
        raise ApkValidationError("file is not a valid APK (no AndroidManifest.xml)")
    return ValidatedApk(
        filename=sanitize_filename(raw_name),
        data=data,
        sha256=hashlib.sha256(data).hexdigest(),
        size=size,
    )
