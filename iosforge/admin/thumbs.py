"""Downscaled previews for the admin's image galleries.

A listing slide is 1290x2796 and weighs several megabytes; a gallery tile shows it
at ~120px. Serving the original for a thumbnail made one tab pull tens of megabytes
on every visit, which is what made the admin feel slow.

Thumbnails are produced once with ffmpeg and cached on disk keyed by the object and
the requested width, so a repeat view costs a file read. The cache is derived data:
losing it only costs one re-encode.
"""

from __future__ import annotations

import hashlib
import subprocess
import tempfile
from pathlib import Path

from iosforge.common.logging import get_logger

log = get_logger("admin.thumbs")

WIDTHS = (160, 320, 640)
DEFAULT_WIDTH = 320
_CACHE = Path(tempfile.gettempdir()) / "iosforge-thumbs"


def clamp_width(requested: int | None) -> int:
    """Snap a requested width to the allowed set.

    Fixed widths keep the cache small and stop an arbitrary query parameter from
    turning into unbounded encoding work.
    """
    if not requested:
        return DEFAULT_WIDTH
    return min(WIDTHS, key=lambda w: abs(w - requested))


def cache_path(key: str, width: int, version: int = 0) -> Path:
    """Where the thumbnail for ``key`` at ``width`` lives.

    ``version`` (the object's byte length) is part of the name so regenerating an
    artifact under the same key — an icon re-roll, a re-rendered slide — cannot
    serve the previous preview from cache.
    """
    digest = hashlib.sha256(f"{key}@{width}@{version}".encode()).hexdigest()[:32]
    return _CACHE / f"{digest}.jpg"


def etag(key: str, width: int, size: int) -> str:
    """Validator that changes when the object or the requested width changes."""
    return hashlib.sha256(f"{key}@{width}@{size}".encode()).hexdigest()[:32]


def render(data: bytes, width: int) -> bytes:
    """Downscale image bytes to ``width``, as JPEG; empty on failure.

    JPEG rather than PNG: these are photographic slides, and the tile is a preview,
    not the artifact — the original stays one click away at full fidelity.
    """
    result = subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-loglevel",
            "error",
            "-i",
            "pipe:0",
            "-vf",
            f"scale={width}:-2:flags=lanczos",
            "-q:v",
            "4",
            "-f",
            "mjpeg",
            "pipe:1",
        ],
        input=data,
        capture_output=True,
        check=False,
    )
    return result.stdout or b""


def thumbnail(key: str, data: bytes, width: int) -> bytes:
    """Cached thumbnail for an artifact, falling back to the original on failure."""
    path = cache_path(key, width, len(data))
    try:
        if path.is_file():
            return path.read_bytes()
    except OSError:
        pass

    small = render(data, width)
    if not small:
        log.warning("thumbs.render_failed", key=key, width=width)
        return data
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(small)
    except OSError as exc:
        log.warning("thumbs.cache_write_failed", key=key, error=str(exc))
    return small
