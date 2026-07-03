"""Turn an uploaded screen-recording into walkthrough screens via ffmpeg.

Replaces the emulator crawl as a screen source: frames are sampled at ``fps``
(dense enough to keep modals, dropdowns and appearance animations visible),
optional ``mpdecimate`` collapses only truly static holds, and the result is
capped to ``max_frames`` by even downsampling. Output is written to match the
crawl contract exactly — PNGs in ``paths.screens_dir`` plus a ``screens.json``
with the same ``id/screenshot/activity/...`` shape ``analyze`` consumes.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from iosforge.common.logging import get_logger

log = get_logger("mvp.video_frames")


def _run_ffmpeg(video: Path, out_dir: Path, fps: int, dedup: bool) -> None:
    vf = f"fps={fps}"
    if dedup:
        vf += ",mpdecimate"
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(video),
        "-vf",
        vf,
        "-fps_mode",
        "vfr",
        "-qscale:v",
        "2",
        str(out_dir / "raw_%05d.png"),
    ]
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=1800, check=False)
    if res.returncode != 0:
        raise RuntimeError(f"ffmpeg frame extraction failed: {res.stderr.strip()}")


def _downsample(frames: list[Path], max_frames: int) -> list[Path]:
    if len(frames) <= max_frames:
        return frames
    step = len(frames) / max_frames
    picked = [frames[min(int(i * step), len(frames) - 1)] for i in range(max_frames)]
    log.info("video_frames.capped", extracted=len(frames), kept=len(picked))
    return picked


def extract_frames(
    video: Path,
    paths: object,
    *,
    fps: int = 4,
    dedup: bool = True,
    max_frames: int = 80,
) -> dict[str, object]:
    """Extract frames from ``video`` into ``paths.screens_dir`` + ``screens.json``.

    ``paths`` is a :class:`iosforge.mvp.paths.RunPaths`; typed as ``object`` to
    avoid a hard import cycle. Returns the parsed screen map.
    """
    screens_dir: Path = paths.screens_dir  # type: ignore[attr-defined]
    screens_json: Path = paths.screens_json  # type: ignore[attr-defined]

    log.info("video_frames.extracting", video=video.name, fps=fps, dedup=dedup)
    _run_ffmpeg(video, screens_dir, fps, dedup)

    raw = sorted(screens_dir.glob("raw_*.png"))
    if not raw:
        raise RuntimeError("ffmpeg produced no frames from the video")
    kept = _downsample(raw, max_frames)

    screens: list[dict[str, object]] = []
    prev_id: str | None = None
    for index, src in enumerate(kept):
        sid = f"{index:04d}"
        name = f"{sid}.png"
        src.rename(screens_dir / name)
        screens.append(
            {
                "id": sid,
                "screenshot": name,
                "activity": "video",
                "signature": sid,
                "elements": [],
                "from": prev_id,
                "tapped": None,
                "navigates_to": [f"{index + 1:04d}"] if index + 1 < len(kept) else [],
            }
        )
        prev_id = sid

    for leftover in screens_dir.glob("raw_*.png"):
        leftover.unlink()

    result = {"package": "video", "screen_count": len(screens), "screens": screens}
    screens_json.write_text(json.dumps(result, indent=2, ensure_ascii=False))
    log.info("video_frames.done", screens=len(screens))
    return result
