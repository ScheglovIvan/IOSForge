"""Level 2 Phase F: content_feed module (subscription-gated library) and plist dedupe."""

from __future__ import annotations

import json
import urllib.request
from pathlib import Path
from typing import Any

import pytest

from iosforge.mvp import functional, swiftui_content, swiftui_gen
from iosforge.mvp import swiftui_capabilities as caps
from iosforge.mvp.paths import RunPaths
from tests.test_compliance_ios import _first_iphone
from tests.test_feasibility import _spec_three_screens

FEED_SCREEN = """import SwiftUI

struct Screen0001View: View {
    @Environment(Router.self) private var router
    @State private var items: [ContentItem] = []
    @State private var opened = ""

    var body: some View {
        VStack(spacing: 12) {
            ForEach(items) { item in
                Button(item.title) {
                    if ContentFeed.open(item) { opened = item.body } else { router.show(.s0000) }
                }
                .accessibilityIdentifier("iosforge.content.item")
            }
            Text(opened)
        }
        .task { items = await ContentFeed.load() }
    }
}
"""


def _spec() -> dict[str, Any]:
    spec = _spec_three_screens()
    spec["content"] = {
        "content_to_seed": [
            {"item": "Morning meditation", "format": "audio", "example": "5 min"},
            {"item": "Premium sleep story", "format": "audio", "example": "20 min"},
        ]
    }
    spec["monetization"] = {"free_vs_premium": [{"feature": "sleep story", "tier": "premium"}]}
    spec["capabilities"] = [
        {"name": "library", "kind": "content_feed", "expected_behavior": "items load",
         "screens": ["0001"], "tier": 2, "module": "content_feed"}
    ]  # fmt: skip
    return spec


def test_seed_marks_premium_items_from_the_spec() -> None:
    items = swiftui_content.seed_items(_spec())
    assert [(i["title"], i["premium"]) for i in items] == [
        ("Morning meditation", False),
        ("Premium sleep story", True),
    ]


def test_feed_renders_bundled_seed_and_operator_url(tmp_path: Path) -> None:
    app = tmp_path / "xcode_app"
    caps.integ.write(
        app, caps.integ.Integrations(content_feed_url="https://cdn.example.com/feed.json")
    )
    swiftui_gen.write_scaffold(app, _spec(), app_name="Calm", bundle_id="dev.iosforge.calm")
    feed = (app / "App/Capabilities/ContentFeed.swift").read_text()
    assert 'static let feedURL = "https://cdn.example.com/feed.json"' in feed
    assert 'title: "Premium sleep story"' in feed and "premium: true)" in feed
    assert "Subscriptions.hasPremium" in feed
    assert any("ContentFeed.load()" in rule for rule in swiftui_gen.module_rules(_spec(), app))


def test_feed_without_operator_url_is_unconfigured(tmp_path: Path) -> None:
    app = tmp_path / "xcode_app"
    swiftui_gen.write_scaffold(app, _spec(), app_name="Calm", bundle_id="dev.iosforge.calm")
    feed = (app / "App/Capabilities/ContentFeed.swift").read_text()
    assert f'static let feedURL = "{swiftui_content.UNCONFIGURED_URL}"' in feed


def test_feed_stub_serves_free_and_premium_items() -> None:
    with swiftui_content.feed_server() as url:
        with urllib.request.urlopen(url, timeout=5) as response:
            items = json.loads(response.read())
    assert [i["premium"] for i in items] == [False, True]


def test_two_modules_emit_one_ats_block() -> None:
    lines = caps._dedupe_properties(
        [
            "        NSAppTransportSecurity:",
            "          NSAllowsLocalNetworking: true",
            '        NSPhotoLibraryUsageDescription: "x"',
            "        NSAppTransportSecurity:",
            "          NSAllowsLocalNetworking: true",
        ]
    )
    assert lines.count("        NSAppTransportSecurity:") == 1 and len(lines) == 3


@pytest.mark.mac
@pytest.mark.skipif(_first_iphone() is None, reason="needs Xcode, XcodeGen and an iPhone simulator")
def test_feed_loads_from_the_stub_and_gates_premium(tmp_path: Path) -> None:
    udid = _first_iphone()
    assert udid is not None
    spec = _spec()
    paths = RunPaths.create(tmp_path / "runs")
    paths.app_spec_json.write_text(json.dumps(spec))
    swiftui_gen.write_scaffold(
        paths.xcode_app, spec, app_name="Calm Demo", bundle_id="dev.iosforge.calm"
    )
    (paths.xcode_app / "App/Features/0001/Screen0001View.swift").write_text(FEED_SCREEN)
    report = functional.run(paths, udid=udid, spec=spec, timeout=1200)
    assert report["ok"], report
    assert {"content.loaded", "content.locked"} <= set(report["checks"][0]["events"])
