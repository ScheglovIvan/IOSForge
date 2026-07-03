"""Deterministic Flutter ad-integration bundle (google_mobile_ads)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from iosforge.mvp import ad_scaffold


def _spec(*, ads: bool) -> dict[str, Any]:
    mon: dict[str, Any] = {"model": "mixed"} if ads else {"model": "free"}
    if ads:
        mon["ad_placements"] = [
            {"format": "rewarded", "trigger": "reward tap", "screen_context": "Rewards"},
            {"format": "banner", "trigger": "home", "screen_context": "Home"},
        ]
    return {"app_name": "T", "monetization": mon}


def test_build_ad_bundle_emits_client_code(tmp_path: Path) -> None:
    out = ad_scaffold.build_ad_bundle(_spec(ads=True), tmp_path / "flutter")
    assert out is not None

    service = (out / "ad_service.dart").read_text()
    assert "google_mobile_ads" in service
    assert "adsEnabled" in service  # pro gate
    for method in ("banner(", "showInterstitial(", "showRewarded("):
        assert method in service

    config = (out / "ad_config.dart").read_text()
    assert "'Rewards':" in config and "'Home':" in config and "'default':" in config

    assert ad_scaffold.GOOGLE_MOBILE_ADS_VERSION in (out / "pubspec_snippet.yaml").read_text()
    assert "GADApplicationIdentifier" in (out / "ios_info_plist_snippet.xml").read_text()
    assert "APPLICATION_ID" in (out / "android_manifest_snippet.xml").read_text()

    integration = (out / "INTEGRATION.md").read_text()
    assert "Rewards" in integration and "rewarded" in integration
    manifest = json.loads((out / "manifest.json").read_text())
    assert set(manifest["formats"]) == {"banner", "rewarded"}
    assert manifest["placements"] == 2


def test_build_ad_bundle_none_without_ads(tmp_path: Path) -> None:
    assert ad_scaffold.build_ad_bundle(_spec(ads=False), tmp_path / "flutter") is None
    assert not (tmp_path / "flutter").exists()


def test_default_placements_when_ads_model_but_no_placements(tmp_path: Path) -> None:
    out = ad_scaffold.build_ad_bundle({"monetization": {"model": "ads"}}, tmp_path / "flutter")
    assert out is not None
    manifest = json.loads((out / "manifest.json").read_text())
    assert set(manifest["formats"]) == {"interstitial", "banner"}  # sensible defaults
