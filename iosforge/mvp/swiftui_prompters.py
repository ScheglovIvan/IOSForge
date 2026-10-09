"""Deterministic ``PermissionPrompter`` sources for every permission kind.

The scaffold renders one ``App/Permissions/<Name>Permission.swift`` per kind the
app_spec declares, so the whole ``App/Permissions/`` directory is contract code
(byte-for-byte verified by the compile gate) and parallel screen tasks never
collide on prompter files. Screens only call ``Permissions.request(<Name>.self)``.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PrompterSource:
    """Swift type name, imports and full declaration body of one prompter."""

    type_name: str
    imports: tuple[str, ...]
    body: str


def _simple(type_name: str, case: str, imports: tuple[str, ...], expr: str) -> PrompterSource:
    body = f"""enum {type_name}: PermissionPrompter {{
    static let kind: PermissionKind = .{case}

    static func prompt() async -> Bool {{
        {expr}
    }}
}}
"""
    return PrompterSource(type_name, imports, body)


def _continuation(type_name: str, case: str, imports: tuple[str, ...], call: str) -> PrompterSource:
    return _simple(
        type_name,
        case,
        imports,
        f"await withCheckedContinuation {{ continuation in\n            {call}\n        }}",
    )


_LOCATION = """@MainActor
final class LocationAuthorizer: NSObject, CLLocationManagerDelegate {
    static let shared = LocationAuthorizer()
    private let manager = CLLocationManager()
    private var continuation: CheckedContinuation<Bool, Never>?

    func request() async -> Bool {
        let status = manager.authorizationStatus
        guard status == .notDetermined else { return Self.granted(status) }
        manager.delegate = self
        return await withCheckedContinuation { continuation in
            self.continuation = continuation
            manager.requestWhenInUseAuthorization()
        }
    }

    nonisolated func locationManagerDidChangeAuthorization(_ manager: CLLocationManager) {
        let status = manager.authorizationStatus
        guard status != .notDetermined else { return }
        Task { @MainActor in
            self.continuation?.resume(returning: Self.granted(status))
            self.continuation = nil
        }
    }

    nonisolated static func granted(_ status: CLAuthorizationStatus) -> Bool {
        status == .authorizedWhenInUse || status == .authorizedAlways
    }
}

enum LocationPermission: PermissionPrompter {
    static let kind: PermissionKind = .location

    static func prompt() async -> Bool {
        await LocationAuthorizer.shared.request()
    }
}
"""

_BLUETOOTH = """@MainActor
final class BluetoothAuthorizer: NSObject, CBCentralManagerDelegate {
    static let shared = BluetoothAuthorizer()
    private var manager: CBCentralManager?
    private var continuation: CheckedContinuation<Bool, Never>?

    func request() async -> Bool {
        guard CBManager.authorization == .notDetermined else {
            return CBManager.authorization == .allowedAlways
        }
        return await withCheckedContinuation { continuation in
            self.continuation = continuation
            manager = CBCentralManager(delegate: self, queue: nil)
        }
    }

    nonisolated func centralManagerDidUpdateState(_ central: CBCentralManager) {
        Task { @MainActor in
            self.continuation?.resume(returning: CBManager.authorization == .allowedAlways)
            self.continuation = nil
        }
    }
}

enum BluetoothPermission: PermissionPrompter {
    static let kind: PermissionKind = .bluetooth

    static func prompt() async -> Bool {
        await BluetoothAuthorizer.shared.request()
    }
}
"""

_HOME_KIT = """@MainActor
final class HomeKitAuthorizer: NSObject, HMHomeManagerDelegate {
    static let shared = HomeKitAuthorizer()
    private var manager: HMHomeManager?
    private var continuation: CheckedContinuation<Bool, Never>?

    func request() async -> Bool {
        await withCheckedContinuation { continuation in
            self.continuation = continuation
            let manager = HMHomeManager()
            manager.delegate = self
            self.manager = manager
        }
    }

    nonisolated func homeManager(
        _ manager: HMHomeManager, didUpdate status: HMHomeManagerAuthorizationStatus
    ) {
        Task { @MainActor in
            self.continuation?.resume(returning: status.contains(.authorized))
            self.continuation = nil
        }
    }
}

enum HomeKitPermission: PermissionPrompter {
    static let kind: PermissionKind = .home_kit

    static func prompt() async -> Bool {
        await HomeKitAuthorizer.shared.request()
    }
}
"""


PROMPTERS: dict[str, PrompterSource] = {
    "notifications": _simple(
        "NotificationsPermission",
        "notifications",
        ("UserNotifications",),
        "(try? await UNUserNotificationCenter.current().requestAuthorization("
        "options: [.alert, .badge, .sound])) ?? false",
    ),
    "tracking": _simple(
        "TrackingPermission",
        "tracking",
        ("AppTrackingTransparency",),
        "await ATTrackingManager.requestTrackingAuthorization() == .authorized",
    ),
    "location": PrompterSource("LocationPermission", ("CoreLocation",), _LOCATION),
    "camera": _simple(
        "CameraPermission",
        "camera",
        ("AVFoundation",),
        "await AVCaptureDevice.requestAccess(for: .video)",
    ),
    "microphone": _simple(
        "MicrophonePermission",
        "microphone",
        ("AVFoundation",),
        "await AVAudioApplication.requestRecordPermission()",
    ),
    "photos": _simple(
        "PhotosPermission",
        "photos",
        ("Photos",),
        "[.authorized, .limited].contains(await PHPhotoLibrary.requestAuthorization("
        "for: .readWrite))",
    ),
    "contacts": _simple(
        "ContactsPermission",
        "contacts",
        ("Contacts",),
        "(try? await CNContactStore().requestAccess(for: .contacts)) ?? false",
    ),
    "calendar": _simple(
        "CalendarPermission",
        "calendar",
        ("EventKit",),
        "(try? await EKEventStore().requestFullAccessToEvents()) ?? false",
    ),
    "reminders": _simple(
        "RemindersPermission",
        "reminders",
        ("EventKit",),
        "(try? await EKEventStore().requestFullAccessToReminders()) ?? false",
    ),
    "health": _simple(
        "HealthPermission",
        "health",
        ("HealthKit",),
        "guard HKHealthStore.isHealthDataAvailable() else { return false }\n"
        "        let read: Set<HKObjectType> = [HKQuantityType(.stepCount)]\n"
        "        return (try? await HKHealthStore().requestAuthorization(toShare: [], read: read))"
        " != nil",
    ),
    "motion": _continuation(
        "MotionPermission",
        "motion",
        ("CoreMotion",),
        "let manager = CMMotionActivityManager()\n"
        "            manager.queryActivityStarting(from: Date(), to: Date(), to: .main) "
        "{ _, error in\n"
        "                _ = manager\n"
        "                continuation.resume(returning: error == nil)\n"
        "            }",
    ),
    "speech": _continuation(
        "SpeechPermission",
        "speech",
        ("Speech",),
        "SFSpeechRecognizer.requestAuthorization { "
        "continuation.resume(returning: $0 == .authorized) }",
    ),
    "bluetooth": PrompterSource("BluetoothPermission", ("CoreBluetooth",), _BLUETOOTH),
    "face_id": _simple(
        "FaceIDPermission",
        "face_id",
        ("LocalAuthentication",),
        "(try? await LAContext().evaluatePolicy("
        '.deviceOwnerAuthenticationWithBiometrics, localizedReason: "Unlock")) ?? false',
    ),
    "media_library": _continuation(
        "MediaLibraryPermission",
        "media_library",
        ("MediaPlayer",),
        "MPMediaLibrary.requestAuthorization { continuation.resume(returning: $0 == .authorized) }",
    ),
    "siri": _continuation(
        "SiriPermission",
        "siri",
        ("Intents",),
        "INPreferences.requestSiriAuthorization { "
        "continuation.resume(returning: $0 == .authorized) }",
    ),
    "focus_status": _continuation(
        "FocusStatusPermission",
        "focus_status",
        ("Intents",),
        "INFocusStatusCenter.default.requestAuthorization { "
        "continuation.resume(returning: $0 == .authorized) }",
    ),
    "home_kit": PrompterSource("HomeKitPermission", ("HomeKit",), _HOME_KIT),
    "local_network": _simple(
        "LocalNetworkPermission",
        "local_network",
        ("Foundation",),
        "false",
    ),
    "nearby_interaction": _simple(
        "NearbyInteractionPermission",
        "nearby_interaction",
        ("NearbyInteraction",),
        "NISession.deviceCapabilities.supportsPreciseDistanceMeasurement",
    ),
    "family_controls": _simple(
        "FamilyControlsPermission",
        "family_controls",
        ("FamilyControls",),
        "(try? await AuthorizationCenter.shared.requestAuthorization(for: .individual)) != nil",
    ),
    "nfc": _simple(
        "NFCPermission",
        "nfc",
        ("CoreNFC",),
        "NFCNDEFReaderSession.readingAvailable",
    ),
    "pasteboard": _simple(
        "PasteboardPermission",
        "pasteboard",
        ("UIKit",),
        "await MainActor.run { UIPasteboard.general.string != nil }",
    ),
    "review": _simple(
        "ReviewPermission",
        "review",
        ("StoreKit", "UIKit"),
        "await MainActor.run {\n"
        "            let scene = UIApplication.shared.connectedScenes\n"
        "                .first { $0.activationState == .foregroundActive } as? UIWindowScene\n"
        "            guard let scene else { return false }\n"
        "            AppStore.requestReview(in: scene)\n"
        "            return true\n"
        "        }",
    ),
}


def render_prompter(case: str) -> tuple[str, str]:
    """``(file name, Swift source)`` of the prompter for permission ``case``."""
    source = PROMPTERS[case]
    imports = "\n".join(f"import {module}" for module in source.imports)
    text = (
        f"{imports}\n\n/// System prompt for `.{case}`. Generated by the IOSForge scaffold — "
        f"do not edit.\n/// Only ever called through `Permissions.request(_:)`.\n{source.body}"
    )
    return f"{source.type_name}.swift", text
