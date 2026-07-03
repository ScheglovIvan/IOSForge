"""Frame routing: app kept, ads preserved+marked, iOS-home/other dropped."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from iosforge.mvp import screen_filter
from iosforge.mvp.paths import RunPaths


def _seed_run(tmp_path: Path, n: int = 4) -> RunPaths:
    paths = RunPaths.create(tmp_path / "run")
    screens = []
    for i in range(n):
        name = f"{i:04d}.png"
        (paths.screens_dir / name).write_bytes(b"\x89PNG\r\n\x1a\n" + bytes([i]))
        screens.append(
            {
                "id": f"{i:04d}",
                "screenshot": name,
                "activity": "video",
                "signature": f"{i:04d}",
                "elements": [],
                "from": f"{i - 1:04d}" if i else None,
                "tapped": None,
                "navigates_to": [f"{i + 1:04d}"] if i + 1 < n else [],
            }
        )
    paths.screens_json.write_text(
        json.dumps({"package": "video", "screen_count": n, "screens": screens})
    )
    return paths


def test_keeps_app_marks_ads_drops_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    paths = _seed_run(tmp_path)
    labels = {
        "0000.png": {"label": "app", "reason": "app screen"},
        "0001.png": {"label": "ad", "reason": "interstitial"},
        "0002.png": {"label": "ios_home", "reason": "springboard"},
        "0003.png": {"label": "app", "reason": "app screen"},
    }
    monkeypatch.setattr(screen_filter, "_classify", lambda paths, timeout: labels)

    audit = screen_filter.filter_screens(paths)

    data = json.loads(paths.screens_json.read_text())
    assert [s["id"] for s in data["screens"]] == ["0000", "0003"]
    assert data["screen_count"] == 2
    assert audit["counts"] == {"total": 4, "kept": 2, "ads": 1, "excluded": 1}

    # ad frame preserved (not discarded) in ad_screens/, home frame dropped
    assert (paths.ad_screens_dir / "0001.png").exists()
    assert (paths.run_dir / "excluded_screens" / "0002.png").exists()
    assert not (paths.screens_dir / "0001.png").exists()
    assert not (paths.screens_dir / "0002.png").exists()
    assert (paths.screens_dir / "0000.png").exists()

    # ad audit carries sequence/trigger context
    ad = audit["ads"][0]
    assert ad["screenshot"] == "0001.png"
    assert ad["ordinal"] == 1
    assert ad["prev_app_id"] == "0000"
    assert ad["next_app_id"] == "0003"  # nearest app after, across the dropped home frame

    kept_0003 = next(s for s in data["screens"] if s["id"] == "0003")
    assert kept_0003["from"] is None  # predecessor was not an app frame
    assert paths.screen_labels_json.exists()


def test_ad_neighbour_context_across_gaps(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    paths = _seed_run(tmp_path, n=5)
    labels = {
        "0000.png": {"label": "app", "reason": ""},
        "0001.png": {"label": "ios_home", "reason": ""},
        "0002.png": {"label": "ad", "reason": ""},
        "0003.png": {"label": "other", "reason": ""},
        "0004.png": {"label": "app", "reason": ""},
    }
    monkeypatch.setattr(screen_filter, "_classify", lambda paths, timeout: labels)

    audit = screen_filter.filter_screens(paths)

    ad = audit["ads"][0]
    assert ad["prev_app_id"] == "0000"  # skips the ios_home frame at index 1
    assert ad["next_app_id"] == "0004"  # skips the other frame at index 3


def test_fail_open_when_everything_flagged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    paths = _seed_run(tmp_path, n=3)
    monkeypatch.setattr(
        screen_filter,
        "_classify",
        lambda paths, timeout: {f"{i:04d}.png": {"label": "ad", "reason": "x"} for i in range(3)},
    )

    audit = screen_filter.filter_screens(paths)

    data = json.loads(paths.screens_json.read_text())
    assert data["screen_count"] == 3  # nothing moved — a mis-classification must not empty the run
    assert audit["counts"]["kept"] == 0
    assert audit["counts"]["ads"] == 3
