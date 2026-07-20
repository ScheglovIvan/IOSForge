"""Deterministic backend-connected Flutter app generation."""

from __future__ import annotations

from pathlib import Path

from iosforge.mvp import flutter_wiring


def test_generate_app_wires_backend(tmp_path: Path) -> None:
    out = flutter_wiring.generate_app(
        {"app_name": "StoryReel"},
        tmp_path / "app",
        project_id="app-scanner-9beab",
        collection_prefix="storyreel_",
        firebase_config={
            "apiKey": "AIza-test",
            "appId": "1:2:web:3",
            "projectId": "app-scanner-9beab",
        },
    )

    pubspec = (out / "pubspec.yaml").read_text()
    for dep in (
        "firebase_core",
        "cloud_firestore",
        "firebase_auth",
        "cloud_functions",
        "video_player",
        "apphud",
        "google_mobile_ads",
        "flutter_riverpod",
    ):
        assert dep in pubspec

    repo = (out / "lib" / "repository.dart").read_text()
    assert "kPrefix = 'storyreel_'" in repo
    assert "storyreel_" not in repo.replace("kPrefix = 'storyreel_'", "")  # prefix only via const
    assert "unlockEpisode" in repo  # calls the server function

    opts = (out / "lib" / "firebase_options.dart").read_text()
    assert "AIza-test" in opts  # real project config baked in

    for f in (
        "main.dart",
        "models.dart",
        "providers.dart",
        "screens/feed.dart",
        "screens/detail.dart",
        "screens/player.dart",
    ):
        assert (out / "lib" / f).exists()
