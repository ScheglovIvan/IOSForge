"""Native distribution integrations: Apphud / Tenjin via SwiftPM, Info.plist, AppIcon."""

from __future__ import annotations

import json
import plistlib
from pathlib import Path
from typing import Any

from PIL import Image

from iosforge.mvp import swiftui_gen
from iosforge.mvp import swiftui_integrations as integ
from iosforge.mvp.swiftui_scaffold import ICON_FILE, ICON_SET, AppIdentity, enforce_contract

SPEC: dict[str, Any] = {
    "app_name": "Demo",
    "screens": [
        {"id": "0011", "name": "Home", "route": "/", "navigates_to": ["0001"]},
        {"id": "0001", "name": "Paywall - Pro"},
    ],
    "navigation": {"type": "stack", "map": []},
    "monetization": {"packages": [{"name": "Pro (Weekly)", "price": "$6.99", "period": "week"}]},
    "permissions": [{"permission": "App Tracking Transparency (ATT)", "reason": "Ads."}],
}


def _configs(tmp_path: Path) -> integ.Integrations:
    (tmp_path / "apphud.json").write_text(
        json.dumps({"sdk_key": "app_key", "placement": "main_demo", "products": ["com.ex.d.pro"]})
    )
    (tmp_path / "tenjin.json").write_text(
        json.dumps({"sdk_key": "TJ", "att_usage_description": "Measure campaigns."})
    )
    (tmp_path / "skan.plist").write_bytes(
        plistlib.dumps({"SKAdNetworkItems": [{"SKAdNetworkIdentifier": "abc.skadnetwork"}]})
    )
    return integ.collect(
        SPEC,
        apphud_config=tmp_path / "apphud.json",
        attribution_config=tmp_path / "tenjin.json",
        skadnetwork_plist=tmp_path / "skan.plist",
        export_compliance_exempt=True,
    )


def test_collect_reads_provisioning_outputs(tmp_path: Path) -> None:
    ints = _configs(tmp_path)
    assert (ints.apphud_key, ints.apphud_placement, ints.tenjin_key) == (
        "app_key",
        "main_demo",
        "TJ",
    )
    assert ints.skadnetwork_ids == ["abc.skadnetwork"]
    assert ints.products == [integ.FixtureProduct("com.ex.d.pro", "Pro (Weekly)", "$6.99", "week")]
    empty = integ.collect(
        SPEC, apphud_config=tmp_path / "x", attribution_config=tmp_path / "y",
        skadnetwork_plist=tmp_path / "skan.plist", export_compliance_exempt=None,
    )  # fmt: skip
    assert not empty.subscriptions and not empty.attribution and empty.skadnetwork_ids == []


def test_subscriptions_and_attribution_templates(tmp_path: Path) -> None:
    ints = _configs(tmp_path)
    apphud = integ.render_subscriptions(ints)
    assert "import ApphudSDK" in apphud and 'static let apiKey = "app_key"' in apphud
    assert (
        "guard !Headless.isActive" in apphud and 'SubscriptionProduct(id: "com.ex.d.pro"' in apphud
    )
    stub = integ.render_subscriptions(integ.Integrations(products=ints.products))
    assert "ApphudSDK" not in stub and "fixtures }" in stub
    tenjin = integ.render_attribution(ints)
    assert (
        "Permissions.request(TrackingPermission.self)" in tenjin and "TenjinSDK.connect()" in tenjin
    )
    assert "TenjinSDK" not in integ.render_attribution(integ.Integrations())


def test_project_and_plist_lines(tmp_path: Path) -> None:
    ints = _configs(tmp_path)
    assert integ.project_packages(ints)[:2] == ["packages:", "  ApphudSDK:"]
    assert "      - package: TenjinSDK" in integ.target_dependencies(ints)
    info = integ.info_properties(ints)
    assert "        ITSAppUsesNonExemptEncryption: false" in info
    assert '          - SKAdNetworkIdentifier: "abc.skadnetwork"' in info
    assert integ.project_packages(integ.Integrations()) == []


def test_scaffold_freezes_integrations_and_draws_an_icon(tmp_path: Path) -> None:
    app = tmp_path / "xcode_app"
    ints = _configs(tmp_path)
    swiftui_gen.write_scaffold(app, SPEC, app_name="Demo", bundle_id="com.ex.d", integrations=ints)
    project = (app / "project.yml").read_text()
    assert "url: https://github.com/apphud/ApphudSDK" in project
    assert "ASSETCATALOG_COMPILER_APPICON_NAME: AppIcon" in project
    assert project.count("NSUserTrackingUsageDescription") == 1
    assert integ.load(app).apphud_key == "app_key"
    icon = Image.open(app / ICON_SET / ICON_FILE)
    assert icon.size == (1024, 1024) and icon.mode == "RGB"
    assert (app / "App/Permissions/TrackingPermission.swift").exists()
    (app / "App/Monetization/Subscriptions.swift").write_text("enum Subscriptions {}\n")
    report = enforce_contract(app, SPEC, AppIdentity("Demo", "com.ex.d"))
    assert report.restored == ["App/Monetization/Subscriptions.swift"]


def test_attribution_declares_the_att_prompter(tmp_path: Path) -> None:
    app = tmp_path / "xcode_app"
    spec = {**SPEC, "permissions": []}
    swiftui_gen.write_scaffold(
        app, spec, app_name="Demo", bundle_id="com.ex.d", integrations=_configs(tmp_path)
    )
    assert "TrackingPermission" in swiftui_gen.prompter_names(spec, app)
    assert swiftui_gen.prompter_names(spec) == []
