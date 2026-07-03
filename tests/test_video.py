"""Video upload validation + ffmpeg frame extraction (replaces APK ingestion)."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from iosforge.admin.video_validate import (
    VideoValidationError,
    sanitize_filename,
    validate,
)
from iosforge.mvp.paths import RunPaths
from iosforge.mvp.video_frames import extract_frames

_MP4 = b"\x00\x00\x00\x18ftypisom" + b"\x00" * 32
_WEBM = b"\x1a\x45\xdf\xa3" + b"\x00" * 32


def test_validate_accepts_mp4() -> None:
    v = validate("Demo.mp4", _MP4, max_bytes=10_000_000)
    assert v.filename == "Demo.mp4"
    assert v.container == "mp4"
    assert v.size == len(_MP4)


def test_validate_accepts_webm() -> None:
    v = validate("clip.webm", _WEBM, max_bytes=10_000_000)
    assert v.container == "webm"


def test_validate_rejects_non_video_and_bad_magic() -> None:
    with pytest.raises(VideoValidationError):
        validate("evil.exe", _MP4, max_bytes=10_000_000)
    with pytest.raises(VideoValidationError):
        validate("fake.mp4", b"not a video at all", max_bytes=10_000_000)
    with pytest.raises(VideoValidationError):
        validate("empty.mp4", b"", max_bytes=10_000_000)
    with pytest.raises(VideoValidationError):
        validate("big.mp4", _MP4, max_bytes=10)


def test_sanitize_filename() -> None:
    assert sanitize_filename("../../etc/passwd") == "passwd.mp4"
    assert "/" not in sanitize_filename("a/b/c.mov")
    assert sanitize_filename("clip.mov").endswith(".mov")


def _make_test_video(dest: Path) -> None:
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc=duration=2:size=320x240:rate=10",
            "-pix_fmt",
            "yuv420p",
            str(dest),
        ],
        check=True,
        timeout=120,
    )


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_extract_frames_writes_screens(tmp_path: Path) -> None:
    video = tmp_path / "rec.mp4"
    _make_test_video(video)
    paths = RunPaths.create(tmp_path / "run")

    result = extract_frames(video, paths, fps=4, dedup=False, max_frames=80)

    pngs = sorted(paths.screens_dir.glob("*.png"))
    assert not list(paths.screens_dir.glob("raw_*.png"))
    assert len(pngs) >= 1
    assert result["screen_count"] == len(pngs)
    assert pngs[0].name == "0000.png"
    assert paths.screens_json.exists()
    screens = result["screens"]
    assert isinstance(screens, list)
    assert screens[0]["activity"] == "video"


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_extract_frames_caps_max_frames(tmp_path: Path) -> None:
    video = tmp_path / "rec.mp4"
    _make_test_video(video)
    paths = RunPaths.create(tmp_path / "run")

    result = extract_frames(video, paths, fps=10, dedup=False, max_frames=3)

    assert result["screen_count"] <= 3
    assert len(sorted(paths.screens_dir.glob("*.png"))) <= 3
