"""Permission-prompt registry and lint for generated SwiftUI apps (SCREEN_NAV_CONTRACT).

Headless screen-id mode must never show a system permission prompt: one alert
covers the screen the Vision Judge is about to capture. Every generated app
therefore routes prompts through a single gate (``Permissions.request`` in the
scaffold's ``App/Permissions/Permissions.swift``), and each concrete prompt lives
in a ``PermissionPrompter`` type under ``App/Permissions/``.

:data:`PERMISSION_KINDS` is the extensible list of prompting iOS APIs, keyed by
the ``PermissionKind`` case the scaffold emits. :func:`permission_violations`
flags any prompting API used outside ``App/Permissions/`` and any direct
``.prompt()`` call that bypasses the gate; the compile gate treats violations as
errors so the fix loop routes them back through ``Permissions.request``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

PERMISSIONS_DIR = "App/Permissions"


@dataclass(frozen=True)
class PermissionKind:
    """One prompting capability: Swift case, Info.plist keys and API patterns."""

    case: str
    plist_keys: tuple[str, ...]
    patterns: tuple[str, ...]
    aliases: tuple[str, ...] = ()


PERMISSION_KINDS: tuple[PermissionKind, ...] = (
    PermissionKind(
        "notifications",
        (),
        (
            r"UNUserNotificationCenter[\s\S]{0,80}?requestAuthorization",
            r"registerForRemoteNotifications",
        ),
        ("notification", "push notification"),
    ),
    PermissionKind(
        "tracking",
        ("NSUserTrackingUsageDescription",),
        (r"ATTrackingManager\s*\.\s*requestTrackingAuthorization",),
        ("att", "app tracking transparency", "idfa"),
    ),
    PermissionKind(
        "location",
        ("NSLocationWhenInUseUsageDescription", "NSLocationAlwaysAndWhenInUseUsageDescription"),
        (
            r"requestWhenInUseAuthorization",
            r"requestAlwaysAuthorization",
            r"requestTemporaryFullAccuracyAuthorization",
            r"CLLocationUpdate\s*\.\s*liveUpdates",
        ),
        ("gps",),
    ),
    PermissionKind(
        "camera",
        ("NSCameraUsageDescription",),
        (r"AVCaptureDevice\s*\.\s*requestAccess\s*\(\s*for:\s*\.video",),
    ),
    PermissionKind(
        "microphone",
        ("NSMicrophoneUsageDescription",),
        (
            r"AVCaptureDevice\s*\.\s*requestAccess\s*\(\s*for:\s*\.audio",
            r"requestRecordPermission",
            r"AVAudioApplication\s*\.\s*requestRecordPermission",
        ),
        ("mic", "audio input", "recording"),
    ),
    PermissionKind(
        "photos",
        ("NSPhotoLibraryUsageDescription", "NSPhotoLibraryAddUsageDescription"),
        (r"PHPhotoLibrary\s*\.\s*requestAuthorization", r"UIImageWriteToSavedPhotosAlbum"),
        ("photo", "photo library", "gallery"),
    ),
    PermissionKind(
        "contacts",
        ("NSContactsUsageDescription",),
        (
            r"CNContactStore\s*\(\s*\)\s*\.\s*requestAccess",
            r"requestAccess\s*\(\s*for:\s*\.contacts",
        ),
    ),
    PermissionKind(
        "calendar",
        ("NSCalendarsFullAccessUsageDescription", "NSCalendarsWriteOnlyAccessUsageDescription"),
        (
            r"requestFullAccessToEvents",
            r"requestWriteOnlyAccessToEvents",
            r"requestAccess\s*\(\s*to:\s*\.event",
        ),
        ("calendars", "events"),
    ),
    PermissionKind(
        "reminders",
        ("NSRemindersFullAccessUsageDescription",),
        (r"requestFullAccessToReminders", r"requestAccess\s*\(\s*to:\s*\.reminder"),
    ),
    PermissionKind(
        "health",
        ("NSHealthShareUsageDescription", "NSHealthUpdateUsageDescription"),
        (
            r"HKHealthStore[\s\S]{0,120}?requestAuthorization",
            r"requestAuthorization\s*\(\s*toShare",
        ),
        ("healthkit",),
    ),
    PermissionKind(
        "motion",
        ("NSMotionUsageDescription",),
        (
            r"CMMotionActivityManager\s*\(",
            r"CMPedometer\s*\(",
            r"CMAltimeter\s*\(",
            r"CMSensorRecorder\s*\(",
        ),
        ("coremotion", "fitness", "pedometer"),
    ),
    PermissionKind(
        "speech",
        ("NSSpeechRecognitionUsageDescription",),
        (r"SFSpeechRecognizer\s*\.\s*requestAuthorization",),
        ("speech recognition",),
    ),
    PermissionKind(
        "bluetooth",
        ("NSBluetoothAlwaysUsageDescription",),
        (r"CBCentralManager\s*\(", r"CBPeripheralManager\s*\("),
    ),
    PermissionKind(
        "face_id",
        ("NSFaceIDUsageDescription",),
        (r"\.evaluatePolicy\s*\(",),
        ("faceid", "biometric", "touch id"),
    ),
    PermissionKind(
        "media_library",
        ("NSAppleMusicUsageDescription",),
        (r"MPMediaLibrary\s*\.\s*requestAuthorization", r"MusicAuthorization\s*\.\s*request"),
        ("apple music", "music library"),
    ),
    PermissionKind(
        "siri",
        ("NSSiriUsageDescription",),
        (r"INPreferences\s*\.\s*requestSiriAuthorization",),
    ),
    PermissionKind(
        "focus_status",
        ("NSFocusStatusUsageDescription",),
        (r"INFocusStatusCenter[\s\S]{0,60}?requestAuthorization",),
        ("focus",),
    ),
    PermissionKind(
        "home",
        ("NSHomeKitUsageDescription",),
        (r"HMHomeManager\s*\(",),
        ("homekit",),
    ),
    PermissionKind(
        "local_network",
        ("NSLocalNetworkUsageDescription",),
        (r"NWBrowser\s*\(", r"NetServiceBrowser\s*\(", r"NWListener\s*\("),
        ("bonjour",),
    ),
    PermissionKind(
        "nearby_interaction",
        ("NSNearbyInteractionUsageDescription",),
        (r"NISession\s*\(",),
        ("uwb",),
    ),
    PermissionKind(
        "family_controls",
        (),
        (r"AuthorizationCenter\s*\.\s*shared\s*\.\s*requestAuthorization",),
        ("screen time", "familycontrols"),
    ),
)

_DIRECT_PROMPT_CALL = re.compile(r"\b[A-Z]\w*Permission\s*\.\s*prompt\s*\(")
_LINE_COMMENT = re.compile(r"//[^\n]*")
_BLOCK_COMMENT = re.compile(r"/\*[\s\S]*?\*/")


def kind_for(name: str) -> PermissionKind | None:
    """Map a free-form ``app_spec.permissions[].permission`` label to a kind."""
    label = name.strip().lower()
    for kind in PERMISSION_KINDS:
        tokens = (kind.case.replace("_", " "), kind.case, *kind.aliases)
        if any(re.search(rf"\b{re.escape(token)}s?\b", label) for token in tokens):
            return kind
    return None


def _strip_comments(source: str) -> str:
    without_blocks = _BLOCK_COMMENT.sub(lambda m: "\n" * m.group(0).count("\n"), source)
    return _LINE_COMMENT.sub("", without_blocks)


def _line_of(source: str, offset: int) -> int:
    return source.count("\n", 0, offset) + 1


def permission_violations(app_dir: Path) -> list[str]:
    """Prompting APIs used outside ``App/Permissions/`` (empty list = clean)."""
    violations: list[str] = []
    allowed = (app_dir / PERMISSIONS_DIR).resolve()
    for swift in sorted((app_dir / "App").rglob("*.swift")):
        rel = swift.relative_to(app_dir)
        source = _strip_comments(swift.read_text(encoding="utf-8", errors="replace"))
        inside_gate = swift.resolve().is_relative_to(allowed)
        if not inside_gate:
            for kind in PERMISSION_KINDS:
                for pattern in kind.patterns:
                    for match in re.finditer(pattern, source):
                        violations.append(
                            f"{rel}:{_line_of(source, match.start())}: error: {kind.case} "
                            f"permission API `{match.group(0).split()[0]}` outside "
                            f"{PERMISSIONS_DIR}/ — move it into a PermissionPrompter and call "
                            "Permissions.request(...)"
                        )
            for match in _DIRECT_PROMPT_CALL.finditer(source):
                violations.append(
                    f"{rel}:{_line_of(source, match.start())}: error: direct "
                    f"`{match.group(0)}` bypasses the headless gate — call "
                    "Permissions.request(...) instead"
                )
    return violations
