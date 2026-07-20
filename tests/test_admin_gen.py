"""Admin/backend deliverable generation (Firebase + Rowy + Apphud + Stream)."""

from __future__ import annotations

import json
from pathlib import Path

from iosforge.mvp.admin_gen import generate_admin


def _spec(*, video: bool, app_type: str) -> dict:
    data_model = [
        {"entity": "User", "fields": ["uid", "coinBalance", "isGuest", "createdAt"]},
    ]
    if video:
        data_model.append(
            {
                "entity": "Episode",
                "fields": ["id", "seriesId", "videoUrl", "isLocked", "unlockCost"],
            }
        )
    else:
        data_model.append({"entity": "Wallpaper", "fields": ["id", "posterUrl", "isPremium"]})
    return {
        "app_name": "TestApp",
        "app_type": app_type,
        "backend": {"admin_panel_needed": True},
        "content": {"data_model": data_model},
        "monetization": {
            "packages": [
                {"name": "Weekly Pro", "price": "$5.99", "period": "week", "includes": ["No ads"]},
                {
                    "name": "Coin pack",
                    "price": "$0.99",
                    "period": "one-time",
                    "includes": ["100 coins"],
                },
            ]
        },
    }


def test_generate_admin_video_app(tmp_path: Path) -> None:
    out = generate_admin(_spec(video=True, app_type="content-subscription"), tmp_path / "admin")

    schema = json.loads((out / "firestore" / "collections.schema.json").read_text())
    assert set(schema["collections"]) == {"User", "Episode"}
    assert schema["collections"]["User"]["coinBalance"] == "number"
    assert schema["collections"]["User"]["isGuest"] == "boolean"

    rules = (out / "firestore" / "firestore.rules").read_text()
    assert "match /User/{uid}" in rules  # user docs are owner-scoped
    assert "allow read, write: if false;" in rules  # deny-by-default catch-all

    products = json.loads((out / "apphud" / "products.json").read_text())
    idents = [p["identifier"] for p in products["products"]]
    assert "weekly_pro" in idents
    sub = next(p for p in products["products"] if p["identifier"] == "weekly_pro")
    assert sub["type"] == "subscription"

    manifest = json.loads((out / "manifest.json").read_text())
    assert manifest["includes_video_stream"] is True
    assert (out / "stream" / "README.md").exists()
    assert (out / "seed" / "seed_empty.py").exists()
    assert (out / "PROVISION.md").exists()
    assert (out / "rowy" / "rowy.config.json").exists()


def test_collection_prefix_namespaces_collections(tmp_path: Path) -> None:
    out = generate_admin(
        _spec(video=True, app_type="content-subscription"),
        tmp_path / "admin",
        collection_prefix="storyreel_",
    )
    schema = json.loads((out / "firestore" / "collections.schema.json").read_text())
    assert set(schema["collections"]) == {"storyreel_User", "storyreel_Episode"}
    rules = (out / "firestore" / "firestore.rules").read_text()
    assert "match /storyreel_User/{uid}" in rules  # prefixed user still owner-scoped
    assert "isOwner(uid)" in rules
    manifest = json.loads((out / "manifest.json").read_text())
    assert manifest["collection_prefix"] == "storyreel_"


def test_generate_admin_non_video_app_has_no_stream(tmp_path: Path) -> None:
    out = generate_admin(_spec(video=False, app_type="utility"), tmp_path / "admin")

    manifest = json.loads((out / "manifest.json").read_text())
    assert manifest["includes_video_stream"] is False
    assert not (out / "stream").exists()
    # no ads model / placements → no ads subdir
    assert not (out / "ads").exists()


def test_generate_admin_emits_ads_config(tmp_path: Path) -> None:
    spec = _spec(video=False, app_type="utility")
    spec["monetization"]["model"] = "mixed"
    spec["monetization"]["ad_placements"] = [
        {"format": "rewarded", "trigger": "reward tap", "screen_context": "Rewards"},
        {"format": "banner", "trigger": "home", "screen_context": "Home"},
    ]
    spec["monetization"]["ad_networks"] = [{"name": "AdMob", "confidence": "low"}]

    out = generate_admin(spec, tmp_path / "admin")

    manifest = json.loads((out / "manifest.json").read_text())
    assert manifest["includes_ads"] is True
    cfg = json.loads((out / "ads" / "admob.config.json").read_text())
    assert len(cfg["ad_units"]) == 2
    assert cfg["pro_disables_ads"] is True
    assert cfg["mediation"]["detected_networks"] == [{"name": "AdMob", "confidence": "low"}]
    rc = json.loads((out / "config" / "remote_config.template.json").read_text())
    assert rc["ads_frequency_interstitial"] == 3
    assert rc["ads_disabled_for_pro"] is True
    # ready Flutter ad client bundle emitted alongside the config
    assert (out / "ads" / "flutter" / "ad_service.dart").exists()
    assert "google_mobile_ads" in (out / "ads" / "flutter" / "pubspec_snippet.yaml").read_text()
