"""Measured loudness envelopes: the waveform must come from the audio, not from taste."""

from __future__ import annotations

import array
import math
import subprocess
from pathlib import Path

from iosforge.mvp import audio_envelope as ae


def _tone(path: Path, expr: str, seconds: float = 2.0) -> Path:
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
            f"aevalsrc={expr}:d={seconds}:s=8000",
            str(path),
        ],
        check=False,
        capture_output=True,
    )
    return path


def test_envelope_tracks_loudness_over_time() -> None:
    # a clip that starts silent and ends loud must produce rising levels
    quiet = array.array("h", [0] * 4000)
    loud = array.array("h", [12000 if i % 2 else -12000 for i in range(4000)])
    levels = ae.envelope(quiet + loud, buckets=8)
    assert levels[0] < 0.2
    assert levels[-1] > 0.9


def test_envelope_is_normalised_to_the_clips_own_peak() -> None:
    # a quiet recording must still draw as a full waveform, not a flat line
    faint = array.array("h", [int(300 * math.sin(i / 5)) for i in range(8000)])
    levels = ae.envelope(faint, buckets=16)
    assert max(levels) == 1.0
    assert all(0.0 <= value <= 1.0 for value in levels)


def test_silence_stays_visible_as_a_floor() -> None:
    # bars of height zero look like a rendering bug, not like silence
    levels = ae.envelope(array.array("h", [0] * 8000), buckets=8)
    assert levels and all(value >= 0.06 for value in levels)


def test_periodicity_detector_catches_a_tiled_pattern() -> None:
    # this is the defect that shipped: eight values repeated eight times, which
    # reads on screen as four identical clumps of bars
    tiled = [0.18, 0.86, 0.97, 0.88, 0.66, 0.44, 0.27, 0.15] * 8
    assert ae.is_periodic(tiled, period=8) is True
    varied = [0.1, 0.9, 0.3, 0.7, 0.2, 0.95, 0.4, 0.55] + [0.5, 0.2, 0.8, 0.35] * 2
    assert ae.is_periodic(varied, period=8) is False


def test_a_real_clip_measures_to_a_non_periodic_envelope(tmp_path: Path) -> None:
    clip = _tone(tmp_path / "sweep.mp3", "0.6*sin(2*PI*t*(200+300*t))")
    levels = ae.measure(clip, buckets=ae.BUCKETS)
    assert len(levels) == ae.BUCKETS
    assert not any(ae.is_periodic(levels, period=p) for p in (4, 8, 16))


def test_two_different_clips_measure_differently(tmp_path: Path) -> None:
    # the whole point: each sound must draw its own shape
    a = ae.measure(_tone(tmp_path / "a.mp3", "0.6*sin(2*PI*t*440)"), buckets=32)
    b = ae.measure(_tone(tmp_path / "b.mp3", "0.6*sin(2*PI*t*220)*t"), buckets=32)
    assert a and b and a != b


def test_unreadable_audio_is_reported_not_raised(tmp_path: Path) -> None:
    broken = tmp_path / "broken.mp3"
    broken.write_bytes(b"not audio")
    assert ae.measure(broken) == []


def test_generated_dart_is_valid_and_named_by_clip() -> None:
    source = ae.to_dart({"tone_water.mp3": [0.1, 0.5, 1.0], "chime.mp3": [0.2, 0.4]})
    assert "kSoundEnvelopes" in source
    assert "'tone_water.mp3': <double>[" in source
    assert "0.100, 0.500, 1.000," in source
    assert source.count("<double>[") == 2
    assert source.rstrip().endswith("};")
    # regenerated on every build, so it must announce itself as generated
    assert "GENERATED" in source


def test_measure_directory_skips_non_audio(tmp_path: Path) -> None:
    audio = tmp_path / "audio"
    _tone(audio / "real.mp3", "0.5*sin(2*PI*t*300)")
    (audio / "notes.txt").write_text("ignore me")
    (audio / ".gitkeep").write_text("")
    measured = ae.measure_directory(audio, buckets=16)
    assert list(measured) == ["real.mp3"]
