"""iOS Simulator rendering for the Vision Judge (Phase 4): pinned environment, stable frames.

Implements ``SIMULATOR_ENV_CONTRACT`` (``providers/base.py``) on top of the mockable
:mod:`iosforge.mvp.xcode` wrapper: the simulator environment (status bar, appearance,
content size) is pinned once per run, every screen is relaunched through the
``-screen-id`` launch argument with the job's locale passed as per-app launch
arguments (``-AppleLanguages`` / ``-AppleLocale``, no reboot), and a screenshot is
accepted only once two consecutive frames differ by less than
:data:`STABLE_FRAME_MAX_DIFF` of their pixels. A missing toolchain raises
:class:`SimulatorUnavailable` — the verify path never passes silently off-Mac.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageChops

from iosforge.mvp import xcode

STABLE_FRAME_MAX_DIFF = 0.005
STABLE_FRAME_TIMEOUT_S = 10.0
STABLE_FRAME_INTERVAL_S = 0.3
PIXEL_NOISE_TOLERANCE = 8
BLANK_DOMINANT_SHARE = 0.999
DEFAULT_APPEARANCE = "light"
DEFAULT_CONTENT_SIZE = "large"


class SimulatorUnavailable(RuntimeError):
    """The Apple toolchain (xcodegen / xcodebuild / xcrun simctl) is not available."""


class UnstableFrame(RuntimeError):
    """A screen never settled under :data:`STABLE_FRAME_MAX_DIFF` before the time-out."""


@dataclass(frozen=True)
class SimEnvironment:
    """Pinned rendering environment: device, locale (BCP-47) and appearance."""

    udid: str
    locale: str
    appearance: str = DEFAULT_APPEARANCE
    content_size: str = DEFAULT_CONTENT_SIZE

    def launch_args(self) -> tuple[str, ...]:
        """Per-app locale launch arguments (``en-US`` → ``(en)`` + ``en_US``)."""
        language, _, region = self.locale.replace("_", "-").partition("-")
        apple_locale = f"{language}_{region.upper()}" if region else language
        return ("-AppleLanguages", f"({language})", "-AppleLocale", apple_locale)


@dataclass(frozen=True)
class StableShot:
    """A stabilised screenshot: where it is, frames taken, last frame-to-frame diff."""

    path: Path
    frames: int
    diff: float
    blank: bool = False


def require_toolchain() -> None:
    """Raise :class:`SimulatorUnavailable` unless running on a Mac with Xcode + XcodeGen."""
    if not xcode.toolchain_available():
        raise SimulatorUnavailable(
            "xcodegen / xcodebuild / xcrun not found: iOS rendering only runs on a Mac worker"
        )


def pin_environment(env: SimEnvironment) -> None:
    """Boot the device and pin status bar, appearance and content size (once per run)."""
    xcode.boot(env.udid)
    xcode.pin_status_bar(env.udid)
    xcode.set_appearance(env.udid, env.appearance)
    xcode.set_content_size(env.udid, env.content_size)


def frame_diff(first: Path, second: Path, *, tolerance: int = PIXEL_NOISE_TOLERANCE) -> float:
    """Share of pixels whose channel difference exceeds ``tolerance`` (1.0 if sizes differ)."""
    with Image.open(first) as a_img, Image.open(second) as b_img:
        a = a_img.convert("RGB")
        b = b_img.convert("RGB")
        if a.size != b.size:
            return 1.0
        delta = (
            ImageChops.difference(a, b)
            .convert("L")
            .point(lambda value: 255 if value > tolerance else 0)
        )
        changed = delta.histogram()[255]
        return changed / float(a.width * a.height)


def near_uniform(path: Path, *, share: float = BLANK_DOMINANT_SHARE) -> bool:
    """True when one grey level covers ``share`` of the content area (status bar and
    home indicator excluded): a launch screen or an empty, not-yet-drawn frame."""
    with Image.open(path) as img:
        grey = img.convert("L")
        content = grey.crop((0, grey.height // 10, grey.width, grey.height * 9 // 10))
        histogram = content.histogram()
        return max(histogram) / float(content.width * content.height) >= share


def capture_stable(
    udid: str,
    out: Path,
    *,
    max_diff: float = STABLE_FRAME_MAX_DIFF,
    timeout_s: float = STABLE_FRAME_TIMEOUT_S,
    interval_s: float = STABLE_FRAME_INTERVAL_S,
) -> StableShot:
    """Screenshot until two consecutive frames differ by < ``max_diff``; else raise.

    A stable but near-uniform frame (nothing drawn yet) is not accepted while time
    remains; if the screen is still blank at the time-out the stable frame is returned
    with ``blank=True`` so ``blank_screens`` / the judge see it, instead of an error.
    """
    out.parent.mkdir(parents=True, exist_ok=True)
    previous = out.with_suffix(".prev.png")
    deadline = time.monotonic() + timeout_s
    frames = 0
    diff = 1.0
    try:
        while True:
            time.sleep(interval_s)
            xcode.screenshot(udid, out)
            frames += 1
            expired = time.monotonic() > deadline
            if frames > 1:
                diff = frame_diff(previous, out)
                if diff < max_diff:
                    blank = near_uniform(out)
                    if not blank or expired:
                        return StableShot(out, frames, diff, blank)
            if expired:
                raise UnstableFrame(
                    f"{out.stem}: no stable frame after {frames} frames "
                    f"(last diff {diff:.4f} >= {max_diff})"
                )
            previous.write_bytes(out.read_bytes())
    finally:
        previous.unlink(missing_ok=True)


def render_screen(env: SimEnvironment, bundle_id: str, screen_id: str, out: Path) -> StableShot:
    """Relaunch the app on ``screen_id`` in ``env`` and return its stable screenshot."""
    xcode.launch_screen(env.udid, bundle_id, screen_id, extra_args=env.launch_args())
    return capture_stable(env.udid, out)
