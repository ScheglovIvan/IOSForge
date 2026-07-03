"""Screen-recording upload validation (SPEC §8 — untrusted file handling).

Checks extension, enforces the size limit, verifies the bytes look like a real
video container (MP4/MOV ``ftyp`` box or Matroska/WebM EBML header), and
sanitizes the filename. The raw video is stored in MinIO (outside any web root)
and only ever read by the worker's ffmpeg frame extractor, never executed.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]")

_EXT_CONTAINER = {
    ".mp4": "mp4",
    ".m4v": "mp4",
    ".mov": "mov",
    ".webm": "webm",
    ".mkv": "mkv",
}
_ALLOWED_EXT = tuple(_EXT_CONTAINER)

_EBML_MAGIC = b"\x1a\x45\xdf\xa3"


class VideoValidationError(Exception):
    """Raised when an uploaded file is not an acceptable video."""


@dataclass
class ValidatedVideo:
    filename: str
    container: str
    data: bytes
    sha256: str
    size: int


def sanitize_filename(raw: str) -> str:
    """Strip any path and reduce to a safe basename ending in an allowed ext."""
    base = raw.replace("\\", "/").split("/")[-1].strip()
    base = _SAFE_NAME.sub("_", base) or "upload.mp4"
    if not base.lower().endswith(_ALLOWED_EXT):
        base += ".mp4"
    return base[:255]


def _looks_like_video(data: bytes, container: str) -> bool:
    if container in ("mp4", "mov"):
        return len(data) >= 12 and data[4:8] == b"ftyp"
    if container in ("webm", "mkv"):
        return data[:4] == _EBML_MAGIC
    return False


def validate(raw_name: str, data: bytes, max_bytes: int) -> ValidatedVideo:
    """Validate uploaded bytes as a screen-recording video, or raise."""
    lower = raw_name.lower()
    ext = next((e for e in _ALLOWED_EXT if lower.endswith(e)), None)
    if ext is None:
        raise VideoValidationError("file must be a video (.mp4, .m4v, .mov, .webm, .mkv)")
    size = len(data)
    if size == 0:
        raise VideoValidationError("file is empty")
    if size > max_bytes:
        raise VideoValidationError(f"file exceeds the {max_bytes} byte limit")
    container = _EXT_CONTAINER[ext]
    if not _looks_like_video(data, container):
        raise VideoValidationError("file is not a valid video container")
    return ValidatedVideo(
        filename=sanitize_filename(raw_name),
        container=container,
        data=data,
        sha256=hashlib.sha256(data).hexdigest(),
        size=size,
    )
