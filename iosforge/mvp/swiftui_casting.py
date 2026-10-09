"""``casting`` / ``screen_mirroring`` capability module: TVs on the local network (Phase E).

Contract code under ``App/Capabilities``:

* ``CastingService.swift`` — REAL discovery with ``NWBrowser`` over Bonjour
  (``_googlecast._tcp``, ``_airplay._tcp``, ``_raop._tcp``), a REAL TCP connection to the
  chosen device with ``NWConnection`` and a stream start (a ``LOAD`` message carrying the
  media URL and title). AirPlay goes through the system route picker (``AirPlayButton``,
  ``AVRoutePickerView``); screen mirroring through the system broadcast picker
  (``ScreenMirrorButton``, ``RPSystemBroadcastPickerView``) bound to the app's ReplayKit
  Broadcast Upload Extension, rendered as its own target (``BroadcastExtension/``).
* Functional mode journals ``cast.discovery_started`` / ``cast.device_found`` /
  ``cast.connected`` / ``cast.stream_started``. The mock is a fake receiver: a TCP server
  on the host advertised with ``dns-sd -R`` as a ``_googlecast._tcp`` device, which the
  simulator discovers through the host's mDNS and which records the ``LOAD`` it receives.

Honest limits: the ``LOAD`` message is the module's own JSON-line protocol, so the check
proves discovery → connection → stream start against the fake receiver, not playback on a
real Chromecast (the Google Cast CASTV2 protocol needs Google's SDK); a ReplayKit broadcast
cannot run on the simulator, so mirroring is built and wired but not functionally driven.
"""

from __future__ import annotations

import json
import secrets
import socket
import subprocess
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from iosforge.mvp import swiftui_capabilities as caps
from iosforge.mvp.swiftui_functional import FunctionalCheck, MockContext, Step, register_mock
from iosforge.mvp.swiftui_templates import DO_NOT_EDIT

KEY = "casting"
MOCK = "casting_receiver"
RECEIVER_NAME = "IOSForge Test TV"
CAST_ID = "iosforge.cast"
DEVICE_ID = "iosforge.cast.device"
EXTENSION_DIR = caps.EXTENSIONS_DIR
EXTENSION_TARGET = "BroadcastExtension"
SERVICE_TYPES = ("_googlecast._tcp", "_airplay._tcp", "_raop._tcp")

CASTING_SWIFT = f"""import AVKit
import Foundation
import Network
import ReplayKit
import SwiftUI

/// A TV or receiver found on the local network.
struct CastDevice: Identifiable, Hashable {{
    let id: String
    let name: String
    let kind: String
    let endpoint: NWEndpoint
}}

/// Local-network casting: Bonjour discovery, a connection to the chosen device and a
/// stream start. Screens use only `CastingService.shared`, `AirPlayButton` and
/// `ScreenMirrorButton`. {DO_NOT_EDIT}
@MainActor
@Observable
final class CastingService {{
    static let shared = CastingService()
    static let serviceTypes = [{", ".join(f'"{t}"' for t in SERVICE_TYPES)}]

    private(set) var devices: [CastDevice] = []
    private(set) var connected: CastDevice?
    private(set) var isSearching = false
    private(set) var lastError: String?

    private var browsers: [NWBrowser] = []
    private var connection: NWConnection?
    private var pending: NWConnection?

    /// In functional mode, the only device whose effects are journaled (the mock receiver).
    private func observed(_ device: CastDevice) -> Bool {{
        guard Functional.isActive else {{ return false }}
        guard let expected = Functional.value("CAST_RECEIVER") else {{ return true }}
        return device.name == expected
    }}

    /// Starts browsing the local network for receivers (results arrive in `devices`).
    func startDiscovery() {{
        guard !Headless.isActive else {{ return }}
        stopDiscovery()
        isSearching = true
        for type in Self.serviceTypes {{
            let browser = NWBrowser(for: .bonjour(type: type, domain: nil), using: .tcp)
            browser.browseResultsChangedHandler = {{ [weak self] results, _ in
                Task {{ @MainActor in self?.update(type: type, results: results) }}
            }}
            browser.stateUpdateHandler = {{ [weak self] state in
                guard case let .failed(error) = state else {{ return }}
                Task {{ @MainActor in
                    self?.lastError = "Searching the network failed."
                    Functional.record("cast.error", ["reason": "browse failed: \\(error)"])
                }}
            }}
            browser.start(queue: .main)
            browsers.append(browser)
        }}
        Functional.record("cast.discovery_started")
    }}

    func stopDiscovery() {{
        browsers.forEach {{ $0.cancel() }}
        browsers = []
        isSearching = false
    }}

    /// Opens a connection to `device`; true when it is ready to receive a stream within
    /// `timeout` seconds (a refused, unreachable or stale device fails instead of hanging).
    @discardableResult
    func connect(_ device: CastDevice, timeout: TimeInterval = 10) async -> Bool {{
        guard !Headless.isActive else {{ return false }}
        pending?.cancel()
        disconnect()
        let candidate = NWConnection(to: device.endpoint, using: .tcp)
        pending = candidate
        let ready = await withCheckedContinuation {{ (continuation: CheckedContinuation<Bool, Never>) in
            var resumed = false
            let finish: (Bool) -> Void = {{ value in
                guard !resumed else {{ return }}
                resumed = true
                continuation.resume(returning: value)
            }}
            candidate.stateUpdateHandler = {{ state in
                switch state {{
                case .ready:
                    finish(true)
                case .failed, .cancelled:
                    finish(false)
                default:
                    break
                }}
            }}
            candidate.start(queue: .main)
            DispatchQueue.main.asyncAfter(deadline: .now() + timeout) {{ finish(false) }}
        }}
        if pending === candidate {{
            pending = nil
        }}
        guard ready else {{
            candidate.stateUpdateHandler = nil
            candidate.cancel()
            lastError = "Could not connect to \\(device.name)."
            Functional.record("cast.error", ["reason": "connect failed", "device": device.name])
            return false
        }}
        candidate.stateUpdateHandler = {{ [weak self] state in
            switch state {{
            case .failed, .cancelled:
                Task {{ @MainActor in self?.dropped(candidate) }}
            default:
                break
            }}
        }}
        lastError = nil
        connection = candidate
        connected = device
        if observed(device) {{
            Functional.record("cast.connected", ["device": device.name])
        }}
        return true
    }}

    /// Asks the connected device to play `mediaURL`; true when the request was delivered.
    @discardableResult
    func cast(mediaURL: URL, title: String) async -> Bool {{
        guard let connection, let device = connected else {{
            lastError = "Connect to a TV first."
            return false
        }}
        let message: [String: String] = ["type": "LOAD", "url": mediaURL.absoluteString, "title": title]
        guard var data = try? JSONSerialization.data(withJSONObject: message) else {{ return false }}
        data.append(0x0A)
        let sent = await withCheckedContinuation {{ (continuation: CheckedContinuation<Bool, Never>) in
            connection.send(content: data, completion: .contentProcessed {{ error in
                continuation.resume(returning: error == nil)
            }})
        }}
        if sent && observed(device) {{
            Functional.record("cast.stream_started", ["device": device.name, "title": title])
        }}
        return sent
    }}

    /// Ends the session with the connected device.
    func disconnect() {{
        connection?.stateUpdateHandler = nil
        connection?.cancel()
        connection = nil
        connected = nil
    }}

    private func dropped(_ lost: NWConnection) {{
        guard connection === lost else {{ return }}
        connection = nil
        connected = nil
        lastError = "The TV disconnected."
    }}

    private func update(type: String, results: Set<NWBrowser.Result>) {{
        var found = devices.filter {{ $0.kind != type }}
        for result in results {{
            guard case let .service(name, _, _, _) = result.endpoint else {{ continue }}
            found.append(CastDevice(id: type + "/" + name, name: name, kind: type, endpoint: result.endpoint))
        }}
        let known = Set(devices.map(\\.id))
        for device in found where !known.contains(device.id) && observed(device) {{
            Functional.record("cast.device_found", ["device": device.name, "kind": device.kind])
        }}
        devices = found.sorted {{ $0.name < $1.name }}
    }}
}}

/// The system AirPlay route picker (TVs and speakers the system can reach).
struct AirPlayButton: UIViewRepresentable {{
    func makeUIView(context: Context) -> AVRoutePickerView {{
        let view = AVRoutePickerView()
        view.prioritizesVideoDevices = true
        return view
    }}

    func updateUIView(_ uiView: AVRoutePickerView, context: Context) {{}}
}}

/// The system screen-broadcast picker bound to the app's broadcast extension (mirroring).
struct ScreenMirrorButton: UIViewRepresentable {{
    func makeUIView(context: Context) -> RPSystemBroadcastPickerView {{
        let view = RPSystemBroadcastPickerView(frame: CGRect(x: 0, y: 0, width: 44, height: 44))
        view.preferredExtension = (Bundle.main.bundleIdentifier ?? "") + ".broadcast"
        view.showsMicrophoneButton = false
        return view
    }}

    func updateUIView(_ uiView: RPSystemBroadcastPickerView, context: Context) {{}}
}}
"""

SAMPLE_HANDLER = f"""import ReplayKit

/// Screen mirroring broadcast: receives the device screen while the user broadcasts.
/// {DO_NOT_EDIT}
final class SampleHandler: RPBroadcastSampleHandler {{
    private var frames = 0

    override func broadcastStarted(withSetupInfo setupInfo: [String: NSObject]?) {{
        frames = 0
    }}

    override func processSampleBuffer(_ sampleBuffer: CMSampleBuffer, with sampleBufferType: RPSampleBufferType) {{
        if sampleBufferType == .video {{
            frames += 1
        }}
    }}

    override func broadcastFinished() {{}}
}}
"""

SCREEN_RULE = (
    "Casting only through the scaffold module (`App/Capabilities/CastingService.swift`): keep\n"
    "  `@State private var casting = CastingService.shared`, call `casting.startDiscovery()` in\n"
    "  `.onAppear`, list `casting.devices` as `Button`s labelled with the device `name` and marked\n"
    f'  `.accessibilityIdentifier("{DEVICE_ID}")` that call `await casting.connect(device)` in a\n'
    "  `Task`, show `casting.connected` / `casting.lastError`, and start the stream from a\n"
    f'  `Button` marked `.accessibilityIdentifier("{CAST_ID}")` that calls\n'
    "  `await casting.cast(mediaURL:title:)`. AirPlay uses `AirPlayButton()`, screen mirroring\n"
    "  `ScreenMirrorButton()`. Never import Network/ReplayKit in screens and never fake devices or\n"
    "  a connection with timers; headless mode shows the fixture devices."
)


def _check(ctx: caps.CapabilityContext) -> FunctionalCheck | None:
    screen = caps.capability_screen(ctx)
    if not screen:
        return None
    return FunctionalCheck(
        name=str(ctx.capability.get("name") or KEY),
        screen_id=screen,
        steps=(
            Step("wait_text", RECEIVER_NAME, timeout=25),
            Step("tap", RECEIVER_NAME, timeout=10, identifier=""),
            Step("pause", timeout=4),
            Step(
                "tap", r"\b(cast|play|start|stream|mirror|send)\b", timeout=10, identifier=CAST_ID
            ),
            Step("pause", timeout=3),
        ),
        expect_events=("cast.device_found", "cast.connected", "cast.stream_started"),
        mock=MOCK,
    )


class FakeReceiver:
    """A TCP receiver advertised on the host as a ``_googlecast._tcp`` device."""

    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []
        self._server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server.bind(("0.0.0.0", 0))
        self._server.listen(4)
        self._server.settimeout(0.5)
        self.port = int(self._server.getsockname()[1])
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                client, _ = self._server.accept()
            except OSError:
                continue
            threading.Thread(target=self._read, args=(client,), daemon=True).start()

    def _read(self, client: socket.socket) -> None:
        buffer = b""
        client.settimeout(1.0)
        with client:
            while not self._stop.is_set():
                try:
                    chunk = client.recv(4096)
                except OSError:
                    continue
                if not chunk:
                    return
                buffer += chunk
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    try:
                        self.messages.append(json.loads(line))
                    except ValueError:
                        continue

    def __enter__(self) -> FakeReceiver:
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._stop.set()
        self._server.close()


@contextmanager
def fake_receiver(name: str = RECEIVER_NAME) -> Iterator[FakeReceiver]:
    """Run :class:`FakeReceiver` and advertise it with ``dns-sd -R`` while the block runs."""
    with FakeReceiver() as receiver:
        advert = subprocess.Popen(
            ["dns-sd", "-R", name, "_googlecast._tcp", "local", str(receiver.port)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            yield receiver
        finally:
            advert.terminate()
            try:
                advert.wait(timeout=5)
            except subprocess.TimeoutExpired:
                advert.kill()
                advert.wait(timeout=5)


@contextmanager
def _mock(check: FunctionalCheck, context: MockContext) -> Iterator[dict[str, str]]:
    name = f"{RECEIVER_NAME} {secrets.token_hex(2).upper()}"
    with fake_receiver(name):
        yield {"IOSFORGE_CAST_RECEIVER": name}


def render_files(ctx: caps.CapabilityContext) -> dict[str, str]:
    """The casting service and the broadcast extension's sample handler."""
    return {
        f"{caps.CAPABILITIES_DIR}/CastingService.swift": CASTING_SWIFT,
        f"{EXTENSION_DIR}/SampleHandler.swift": SAMPLE_HANDLER,
    }


def extension_target(app_target: str, bundle_id: str) -> list[str]:
    """XcodeGen lines of the ReplayKit Broadcast Upload Extension target."""
    return [
        f"  {EXTENSION_TARGET}:",
        "    type: app-extension",
        "    platform: iOS",
        f"    sources: [{EXTENSION_DIR}]",
        "    settings:",
        "      base:",
        f"        PRODUCT_BUNDLE_IDENTIFIER: {bundle_id}.broadcast",
        "        GENERATE_INFOPLIST_FILE: YES",
        '        MARKETING_VERSION: "1.0"',
        '        CURRENT_PROJECT_VERSION: "1"',
        "        CODE_SIGNING_ALLOWED: NO",
        '        SWIFT_VERSION: "5.0"',
        '        TARGETED_DEVICE_FAMILY: "1"',
        "    info:",
        f"      path: {EXTENSION_DIR}/Info.plist",
        "      properties:",
        "        CFBundleDisplayName: Screen Mirroring",
        "        CFBundleShortVersionString: $(MARKETING_VERSION)",
        "        CFBundleVersion: $(CURRENT_PROJECT_VERSION)",
        "        NSExtension:",
        "          NSExtensionPointIdentifier: com.apple.broadcast-services-upload",
        "          NSExtensionPrincipalClass: $(PRODUCT_MODULE_NAME).SampleHandler",
        "          RPBroadcastProcessMode: RPBroadcastProcessModeSampleBuffer",
    ]


def _properties(ctx: caps.CapabilityContext) -> list[str]:
    return [
        '        NSLocalNetworkUsageDescription: "Find TVs and receivers on your Wi-Fi to cast to."',
        "        NSBonjourServices:",
        *[f"          - {t}" for t in SERVICE_TYPES],
    ]


DESCRIPTOR = caps.CapabilityDescriptor(
    key=KEY,
    directory=caps.CAPABILITIES_DIR,
    render=render_files,
    screen_api_rule=SCREEN_RULE,
    info_properties=_properties,
    functional_check=_check,
    extra_targets=lambda ctx, target, bundle: extension_target(target, bundle),
    embedded_targets=(EXTENSION_TARGET,),
)


def register() -> None:
    """Register the casting module and its fake receiver."""
    caps.register(DESCRIPTOR)
    register_mock(MOCK, _mock)
