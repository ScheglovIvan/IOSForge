"""Placeholder catalog + ffmpeg dummy-media generation."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from iosforge.mvp import content_seed


def test_placeholder_catalog_is_deterministic() -> None:
    cat = content_seed.placeholder_catalog({"app_name": "StoryReel"}, series_n=3, episodes_n=5)
    assert len(cat["series"]) == 3
    assert len(cat["episodes"]) == 15
    assert cat["series"][0]["episodeCount"] == 5
    assert cat["series"][0]["freeEpisodeCount"] == 2
    ep0 = cat["episodes"][0]
    assert ep0["isLocked"] is False and ep0["unlockCost"] == 0  # first two free
    ep3 = cat["episodes"][3]
    assert ep3["isLocked"] is True and ep3["unlockCost"] == 20
    assert "StoryReel" in cat["series"][0]["title"]


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_build_media_generates_clips_and_posters(tmp_path: Path) -> None:
    cat = content_seed.placeholder_catalog({"app_name": "T"}, series_n=2, episodes_n=2)
    media = content_seed.build_media(tmp_path / "media", cat)

    assert (tmp_path / "media" / "series_000.png").exists()
    assert (tmp_path / "media" / "series_000_ep_000.mp4").exists()
    # a real, non-empty video file
    clip = media["clip:series_000_ep_000"]
    assert clip.stat().st_size > 0
    poster = media["poster:series_000"]
    assert poster.stat().st_size > 0


def test_bucket_name() -> None:
    assert content_seed._bucket_name("app-scanner-9beab") == "app-scanner-9beab.firebasestorage.app"
