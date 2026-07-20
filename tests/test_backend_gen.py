"""Deployable Cloud Functions package generation (real server logic)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from iosforge.mvp import backend_gen


def _spec(*, backend: bool) -> dict[str, Any]:
    return {
        "app_name": "StoryReel",
        "backend": {"backend_needed": backend},
        "content": {"data_model": [{"entity": "Episode", "fields": ["id", "videoUrl"]}]},
        "monetization": {"model": "mixed"},
    }


def test_generate_backend_emits_deployable_functions(tmp_path: Path) -> None:
    fns = backend_gen.generate_backend(
        _spec(backend=True),
        tmp_path / "admin",
        collection_prefix="storyreel_",
        project_id="app-scanner-9beab",
    )
    assert fns is not None

    index = (fns / "src" / "index.ts").read_text()
    for fn in ("unlockEpisode", "grantReward", "signedVideoUrl", "apphudWebhook"):
        assert f"export const {fn}" in index
    assert "runTransaction" in index  # server-enforced wallet, not client-tamperable

    config = (fns / "src" / "config.ts").read_text()
    assert "storyreel_User" in config and "storyreel_Episode" in config

    pkg = json.loads((fns / "package.json").read_text())
    assert "firebase-functions" in pkg["dependencies"]

    admin_dir = tmp_path / "admin"
    fb = json.loads((admin_dir / "firebase.json").read_text())
    assert fb["functions"]["source"] == "functions"
    assert (admin_dir / ".firebaserc").read_text().find("app-scanner-9beab") != -1
    assert (admin_dir / "storage.rules").exists()


def test_generate_backend_none_without_backend(tmp_path: Path) -> None:
    assert backend_gen.generate_backend(_spec(backend=False), tmp_path / "admin") is None
    assert not (tmp_path / "admin" / "functions").exists()
