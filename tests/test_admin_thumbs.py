"""Gallery thumbnails: the admin must not ship megabytes to draw a 120px tile."""

from __future__ import annotations

import subprocess
from pathlib import Path

from iosforge.admin import thumbs


def _slide(path: Path, width: int = 1290, height: int = 2796) -> bytes:
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"gradients=s={width}x{height}:c0=magenta:c1=orange",
            "-frames:v",
            "1",
            str(path),
        ],
        check=False,
        capture_output=True,
    )
    return path.read_bytes()


def test_thumbnail_is_a_fraction_of_the_original(tmp_path: Path) -> None:
    original = _slide(tmp_path / "slide.png")
    small = thumbs.render(original, 320)
    assert small
    # a gallery of four slides used to cost ~16 MB; the point of this module is
    # that a tile costs orders of magnitude less
    assert len(small) < len(original) / 10


def test_thumbnail_keeps_the_aspect_ratio(tmp_path: Path) -> None:
    original = _slide(tmp_path / "slide.png")
    out = tmp_path / "thumb.jpg"
    out.write_bytes(thumbs.render(original, 320))
    probe = subprocess.run(
        [
            "ffprobe",
            "-loglevel",
            "error",
            "-show_entries",
            "stream=width,height",
            "-of",
            "csv=p=0",
            str(out),
        ],
        check=False,
        capture_output=True,
        text=True,
    ).stdout.strip()
    width, height = (int(v) for v in probe.split(",")[:2])
    assert width == 320
    assert abs(height / width - 2796 / 1290) < 0.02


def test_widths_are_a_fixed_set_so_the_cache_stays_bounded() -> None:
    # an arbitrary ?w= would let a caller drive unbounded encoding work
    assert thumbs.clamp_width(300) == 320
    assert thumbs.clamp_width(10_000) == 640
    assert thumbs.clamp_width(None) == thumbs.DEFAULT_WIDTH
    assert thumbs.clamp_width(0) == thumbs.DEFAULT_WIDTH


def test_second_request_is_served_from_cache(tmp_path: Path) -> None:
    original = _slide(tmp_path / "slide.png")
    key = "jobs/abc/store_assets/02.png"
    first = thumbs.thumbnail(key, original, 160)
    assert thumbs.cache_path(key, 160, len(original)).is_file()
    assert thumbs.thumbnail(key, original, 160) == first


def test_regenerated_artifact_is_not_served_from_the_old_cache(tmp_path: Path) -> None:
    # an icon re-roll or a re-rendered slide keeps the same key; the preview must
    # follow the new bytes rather than the previous run's
    key = "jobs/abc/app_icon/app_icon.png"
    first = _slide(tmp_path / "a.png", 600, 600)
    second = _slide(tmp_path / "b.png", 900, 900)
    assert thumbs.cache_path(key, 160, len(first)) != thumbs.cache_path(key, 160, len(second))


def test_etag_changes_with_the_object_and_the_width() -> None:
    base = thumbs.etag("k", 320, 100)
    assert thumbs.etag("k", 320, 101) != base  # regenerated slide
    assert thumbs.etag("k", 640, 100) != base  # different size requested
    assert thumbs.etag("k", 320, 100) == base  # stable otherwise


def test_unreadable_bytes_fall_back_to_the_original() -> None:
    # a preview that cannot be made must not blank the gallery
    assert thumbs.thumbnail("jobs/x/y.png", b"not an image", 320) == b"not an image"


def test_a_past_version_gets_its_own_preview_identity() -> None:
    # the icon's key never changes, so a version's preview must not collide with
    # the current one's — the route mixes the version id into the cache key
    key = "jobs/abc/app_icon/app_icon.png"
    assert thumbs.etag(f"{key}@v1", 160, 100) != thumbs.etag(key, 160, 100)
    assert thumbs.cache_path(f"{key}@v1", 160, 100) != thumbs.cache_path(key, 160, 100)
