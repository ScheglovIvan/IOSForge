"""Global no-ads rule: spec stripping and the compile-gate ad check."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from iosforge.mvp.swiftui_ads import ad_violations, is_ad_component, strip_ad_components


def _write(app: Path, rel: str, text: str) -> None:
    path = app / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


@pytest.mark.parametrize(
    "component",
    [
        {"type": "ad_banner", "role": "monetization"},
        {"type": "native_ad", "role": "feed"},
        {"type": "overlay", "role": "interstitial"},
        {"type": "button", "role": "rewarded"},
        {"type": "text", "role": "disclaimer", "data": "'This action may contain Ads'"},
        {"type": "loading_overlay", "role": "spinner", "data": "Loading ads..."},
        {"type": "AdBanner"},
        {"type": "NativeAdView"},
        {"type": "view", "role": "bannerAd"},
    ],
)
def test_ad_components_are_detected(component: dict[str, Any]) -> None:
    assert is_ad_component(component)


@pytest.mark.parametrize(
    "component",
    [
        {"type": "button", "role": "add_item", "data": "Add"},
        {"type": "card", "role": "paywall", "data": "Unlock everything, 3-day trial"},
        {"type": "banner", "role": "upsell", "data": "Get Pro"},
        {"type": "text", "role": "header", "data": "Address book"},
        {"type": "paywall", "role": "paywall", "data": "Unlimited scans; Remove ads; Weekly"},
        {"type": "feature_row", "role": "benefit", "data": "Ad-free experience"},
        {"type": "gadget_card", "role": "promo", "data": "Gadget picks"},
    ],
)
def test_regular_components_are_kept(component: dict[str, Any]) -> None:
    assert not is_ad_component(component)


def test_strip_ad_components_returns_a_clean_copy() -> None:
    spec = {
        "screens": [
            {
                "id": "0000",
                "components": [
                    {"type": "logo"},
                    {"type": "text", "role": "disclaimer", "data": "This action may contain Ads"},
                ],
            }
        ],
        "monetization": {"model": "subscription", "ad_networks": ["admob"], "ads": [{"x": 1}]},
    }
    clean, removed = strip_ad_components(spec)
    assert removed == 1
    assert clean["screens"][0]["components"] == [{"type": "logo"}]
    assert clean["monetization"] == {"model": "subscription", "ad_networks": [], "ads": []}
    assert len(spec["screens"][0]["components"]) == 2


@pytest.mark.parametrize(
    ("source", "needle"),
    [
        ("import GoogleMobileAds\n", "ad SDK `GoogleMobileAds`"),
        ("let banner = GADBannerView(adSize: size)\n", "ad SDK type `GADBannerView`"),
        ('Text("This action may contain Ads")\n', "ad UI text"),
        ('Text("Loading ads…")\n', "ad UI text"),
        ('Text("Ad").font(.caption)\n', "ad UI text"),
        ('Button("Watch an ad to unlock") {}\n', "ad UI text"),
        ('Label("Remove Ads", systemImage: "xmark")\n', "ad UI text"),
        ('Text("Sponsored")\n', "ad UI text"),
        ('/* " */ Text("Sponsored")\n', "ad UI text"),
        ('Text("Ads by Example")\n', "ad UI text"),
        ('Button("Watch video to unlock") {}\n', "ad UI text"),
        ("@_exported import AppLovinSDK\n", "ad SDK `AppLovinSDK`"),
        ("import GoogleMobileAdsMediationMeta\n", "ad SDK `GoogleMobileAdsMediationMeta`"),
    ],
)
def test_ad_code_and_text_fail_the_gate(tmp_path: Path, source: str, needle: str) -> None:
    _write(tmp_path, "App/Features/0000/Screen0000View.swift", source)
    violations = ad_violations(tmp_path)
    assert len(violations) == 1 and needle in violations[0]
    assert violations[0].startswith("App/Features/0000/Screen0000View.swift:1: error:")


def test_regular_copy_and_comments_pass(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "App/Features/0001/Screen0001View.swift",
        '// "Loading ads" used to sit here\n'
        'Text("Add to playlist")\nText("Upgrade to Pro")\nText("Adjust the volume")\n'
        "let adjusted = loadAddress()\n",
    )
    assert ad_violations(tmp_path) == []


def test_paywall_keeps_its_component_but_loses_the_ad_phrase() -> None:
    spec = {
        "screens": [
            {
                "id": "0001",
                "components": [
                    {
                        "type": "paywall",
                        "role": "paywall",
                        "data": "Unlimited scans; Remove ads; Weekly",
                    }
                ],
            }
        ]
    }
    clean, removed = strip_ad_components(spec)
    assert removed == 0
    assert clean["screens"][0]["components"][0]["data"] == "Unlimited scans; Weekly"


def test_multiline_string_ad_text_fails_the_gate(tmp_path: Path) -> None:
    _write(tmp_path, "App/Features/0000/Screen0000View.swift", 'let s = """\nLoading ads\n"""\n')
    assert len(ad_violations(tmp_path)) == 1


def test_gadget_identifier_is_not_an_ad_type(tmp_path: Path) -> None:
    _write(tmp_path, "App/Features/0000/Screen0000View.swift", "let GADGET = 1\nlet x = GADGET\n")
    assert ad_violations(tmp_path) == []
