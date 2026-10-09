"""Measure the loudness envelope of the app's own audio assets.

The waveform on a cleaning screen should be the shape of the sound the user is
hearing. Asking a code model to invent that shape produces plausible-looking
numbers that are secretly periodic — the first attempt repeated an eight-value
pattern eight times, which reads on screen as four identical clumps of bars.

So the numbers are measured instead of written. ffmpeg decodes each clip to mono
PCM, the samples are folded into a fixed number of windows, and each window keeps
its RMS level. The result is deterministic (same file, same numbers), needs no
audio library at runtime, and cannot be periodic unless the sound itself is.
"""

from __future__ import annotations

import array
import math
import subprocess
from pathlib import Path

from iosforge.common.logging import get_logger

log = get_logger("mvp.audio_envelope")

BUCKETS = 64
_SAMPLE_RATE = 8000
_FLOOR = 0.06


def decode_pcm(path: Path, *, rate: int = _SAMPLE_RATE, timeout: int = 120) -> array.array[int]:
    """Decode an audio file to mono 16-bit PCM samples."""
    result = subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-i",
            str(path),
            "-ac",
            "1",
            "-ar",
            str(rate),
            "-f",
            "s16le",
            "-",
        ],
        capture_output=True,
        check=False,
        timeout=timeout,
    )
    samples = array.array("h")
    payload = result.stdout
    samples.frombytes(payload[: len(payload) - (len(payload) % 2)])
    return samples


def envelope(samples: array.array[int], buckets: int = BUCKETS) -> list[float]:
    """RMS level per window, normalised to the clip's own peak.

    Normalising against the loudest window rather than full scale keeps a quiet
    recording from drawing as a flat line; the floor keeps silent stretches
    visible as short bars instead of nothing at all.
    """
    if not samples or buckets <= 0:
        return []
    size = max(1, len(samples) // buckets)
    levels: list[float] = []
    for index in range(buckets):
        window = samples[index * size : (index + 1) * size]
        if not window:
            levels.append(0.0)
            continue
        total = math.fsum(float(value) * float(value) for value in window)
        levels.append(math.sqrt(total / len(window)))

    peak = max(levels) or 1.0
    return [round(max(_FLOOR, level / peak), 3) for level in levels]


def measure(path: Path, buckets: int = BUCKETS) -> list[float]:
    """The loudness envelope of one audio file; empty when it cannot be read."""
    try:
        levels = envelope(decode_pcm(path), buckets)
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("audio_envelope.failed", path=str(path), error=str(exc))
        return []
    if not levels:
        log.warning("audio_envelope.empty", path=str(path))
    return levels


def is_periodic(levels: list[float], *, period: int, tolerance: float = 0.02) -> bool:
    """Whether ``levels`` simply repeats every ``period`` entries.

    The defect this module exists to prevent: a hand-written envelope that looks
    varied but is one short pattern tiled across the width.
    """
    if period <= 0 or len(levels) < period * 2:
        return False
    for index in range(period, len(levels)):
        if abs(levels[index] - levels[index - period]) > tolerance:
            return False
    return True


def measure_directory(audio_dir: Path, buckets: int = BUCKETS) -> dict[str, list[float]]:
    """Envelopes for every clip in ``audio_dir``, keyed by file name."""
    out: dict[str, list[float]] = {}
    for path in sorted(audio_dir.glob("*")):
        if path.suffix.lower() not in {".mp3", ".m4a", ".wav", ".aac", ".caf"}:
            continue
        levels = measure(path, buckets)
        if levels:
            out[path.name] = levels
    log.info("audio_envelope.measured", clips=len(out), dir=str(audio_dir))
    return out
