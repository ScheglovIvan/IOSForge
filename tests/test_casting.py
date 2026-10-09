"""Level 2 Phase E: casting module — real Bonjour discovery against a fake TV receiver."""

from __future__ import annotations

import json
import socket
import time
from pathlib import Path
from typing import Any

import pytest

from iosforge.mvp import functional, swiftui_casting, swiftui_gen
from iosforge.mvp import swiftui_capabilities as caps
from iosforge.mvp.paths import RunPaths
from tests.test_compliance_ios import _first_iphone
from tests.test_feasibility import _spec_three_screens

CAST_SCREEN = """import SwiftUI

struct Screen0001View: View {
    @State private var casting = CastingService.shared

    var body: some View {
        VStack(spacing: 12) {
            ForEach(casting.devices) { device in
                Button(device.name) { Task { await casting.connect(device) } }
                    .accessibilityIdentifier("iosforge.cast.device")
            }
            Text(casting.connected?.name ?? "Not connected")
            Button("Cast") {
                Task {
                    let media = URL(string: "https://example.com/v.mp4")!
                    await casting.cast(mediaURL: media, title: "Demo")
                }
            }
            .accessibilityIdentifier("iosforge.cast")
            AirPlayButton().frame(width: 44, height: 44)
            ScreenMirrorButton().frame(width: 44, height: 44)
        }
        .onAppear { casting.startDiscovery() }
    }
}
"""

DECOY_SCREEN = CAST_SCREEN.replace(
    "await casting.connect(device)",
    'await casting.connect(casting.devices.first { $0.name.hasPrefix("Decoy") } ?? device)',
)


def _spec() -> dict[str, Any]:
    spec = _spec_three_screens()
    spec["capabilities"] = [
        {"name": "cast_to_tv", "kind": "casting", "expected_behavior": "TVs listed, cast starts",
         "screens": ["0001"], "tier": 2, "module": "casting"}
    ]  # fmt: skip
    return spec


def test_casting_renders_service_extension_and_plist(tmp_path: Path) -> None:
    app = tmp_path / "xcode_app"
    swiftui_gen.write_scaffold(app, _spec(), app_name="Cast", bundle_id="dev.iosforge.cast")
    assert "NWBrowser(for: .bonjour" in (app / "App/Capabilities/CastingService.swift").read_text()
    assert (
        "RPBroadcastSampleHandler" in (app / "BroadcastExtension/SampleHandler.swift").read_text()
    )
    project = (app / "project.yml").read_text()
    assert "  BroadcastExtension:\n    type: app-extension" in project
    assert "      - target: BroadcastExtension" in project
    assert "NSLocalNetworkUsageDescription" in project and "          - _googlecast._tcp" in project
    assert "com.apple.broadcast-services-upload" in project


def test_fake_receiver_records_load_messages() -> None:
    with swiftui_casting.FakeReceiver() as receiver:
        with socket.create_connection(("127.0.0.1", receiver.port), timeout=5) as client:
            client.sendall(b'{"type": "LOAD", "url": "u", "title": "t"}\n')
        deadline = time.time() + 5
        while not receiver.messages and time.time() < deadline:
            time.sleep(0.1)
    assert receiver.messages == [{"type": "LOAD", "url": "u", "title": "t"}]


def test_functional_check_drives_discover_connect_cast() -> None:
    ctx = caps.CapabilityContext(_spec(), caps.integ.Integrations(), _spec()["capabilities"][0])
    check = swiftui_casting.DESCRIPTOR.functional_check(ctx)
    assert check is not None and check.mock == swiftui_casting.MOCK
    assert check.expect_events == ("cast.device_found", "cast.connected", "cast.stream_started")


def test_mock_names_a_unique_receiver_for_the_app(monkeypatch: pytest.MonkeyPatch) -> None:
    from contextlib import contextmanager

    names: list[str] = []

    @contextmanager
    def quiet(name: str = swiftui_casting.RECEIVER_NAME) -> Any:
        names.append(name)
        yield None

    monkeypatch.setattr(swiftui_casting, "fake_receiver", quiet)
    check = swiftui_casting.DESCRIPTOR.functional_check(
        caps.CapabilityContext(_spec(), caps.integ.Integrations(), _spec()["capabilities"][0])
    )
    assert check is not None
    context = functional.swf.MockContext(udid="U", bundle_id="b")
    with (
        swiftui_casting._mock(check, context) as first,
        swiftui_casting._mock(check, context) as again,
    ):
        assert first["IOSFORGE_CAST_RECEIVER"] == names[0] != names[1]
        assert again["IOSFORGE_CAST_RECEIVER"].startswith(swiftui_casting.RECEIVER_NAME)


def test_service_journals_only_the_mock_receiver_and_times_out() -> None:
    swift = swiftui_casting.CASTING_SWIFT
    assert 'Functional.value("CAST_RECEIVER")' in swift
    assert "where !known.contains(device.id) && observed(device)" in swift
    assert "if sent && observed(device)" in swift and "if observed(device)" in swift
    assert "asyncAfter(deadline: .now() + timeout)" in swift and "pending?.cancel()" in swift
    assert "private func dropped(" in swift


def _cast_run(tmp_path: Path, screen: str) -> dict[str, Any]:
    udid = _first_iphone()
    assert udid is not None
    spec = _spec()
    paths = RunPaths.create(tmp_path / "runs")
    paths.app_spec_json.write_text(json.dumps(spec))
    swiftui_gen.write_scaffold(
        paths.xcode_app, spec, app_name="Cast Demo", bundle_id="dev.iosforge.castdemo"
    )
    (paths.xcode_app / "App/Features/0001/Screen0001View.swift").write_text(screen)
    return functional.run(paths, udid=udid, spec=spec, timeout=1500)


@pytest.mark.mac
@pytest.mark.skipif(_first_iphone() is None, reason="needs Xcode, XcodeGen and an iPhone simulator")
def test_connecting_to_another_tv_fails_the_check(tmp_path: Path) -> None:
    with swiftui_casting.fake_receiver("Decoy TV") as decoy:
        check = _cast_run(tmp_path, DECOY_SCREEN)["checks"][0]
        assert decoy.messages and decoy.messages[0]["type"] == "LOAD"
    assert not check["passed"] and check["ui_passed"] and not check["infra_error"]
    assert "cast.connected" in check["missing_events"]


@pytest.mark.mac
@pytest.mark.skipif(_first_iphone() is None, reason="needs Xcode, XcodeGen and an iPhone simulator")
def test_app_discovers_the_fake_tv_and_starts_a_stream(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    udid = _first_iphone()
    assert udid is not None
    received: list[dict[str, Any]] = []
    original = swiftui_casting.fake_receiver

    from contextlib import contextmanager

    @contextmanager
    def recording(name: str = swiftui_casting.RECEIVER_NAME) -> Any:
        with original(name) as receiver:
            yield receiver
            received.extend(receiver.messages)

    monkeypatch.setattr(swiftui_casting, "fake_receiver", recording)
    report = _cast_run(tmp_path, CAST_SCREEN)
    assert report["ok"], report
    assert received and received[0]["type"] == "LOAD"
