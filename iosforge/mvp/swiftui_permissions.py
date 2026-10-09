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
            r"UNUserNotificationCenter\s*\.\s*current\s*\(\s*\)\s*\.\s*requestAuthorization",
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
            r"CLServiceSession\b",
        ),
        ("gps",),
    ),
    PermissionKind(
        "camera",
        ("NSCameraUsageDescription",),
        (
            r"requestAccess\s*\(\s*for:\s*(?:AVMediaType)?\s*\.video",
            r"AVCaptureSession\s*\(",
            r"UIImagePickerController\b",
            r"\bARSession\s*\(",
            r"\bARView\s*\(",
            r"DataScannerViewController\b",
        ),
    ),
    PermissionKind(
        "microphone",
        ("NSMicrophoneUsageDescription",),
        (
            r"requestAccess\s*\(\s*for:\s*(?:AVMediaType)?\s*\.audio",
            r"requestRecordPermission",
            r"AVAudioRecorder\s*\(",
            r"\.inputNode\b",
        ),
        ("mic", "audio input", "voice recording"),
    ),
    PermissionKind(
        "photos",
        ("NSPhotoLibraryUsageDescription", "NSPhotoLibraryAddUsageDescription"),
        (
            r"PHPhotoLibrary\s*\.\s*requestAuthorization",
            r"UIImageWriteToSavedPhotosAlbum",
            r"PHPhotoLibrary\s*\.\s*shared\s*\(\s*\)\s*\.\s*performChanges",
        ),
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
            r"HKHealthStore\s*\(\s*\)\s*\.\s*requestAuthorization",
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
        (r"INFocusStatusCenter\s*\.\s*default\s*\.\s*requestAuthorization",),
        ("focus",),
    ),
    PermissionKind(
        "home_kit",
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
    PermissionKind(
        "nfc",
        ("NFCReaderUsageDescription",),
        (r"NFC\w*ReaderSession\s*\(",),
    ),
    PermissionKind(
        "pasteboard",
        (),
        (r"UIPasteboard\s*\.\s*general\s*\.\s*(?:string|strings|image|images|url|urls|items)\b",),
        ("clipboard", "paste"),
    ),
    PermissionKind(
        "review",
        (),
        (r"\brequestReview\b",),
        ("app review", "rating prompt"),
    ),
)

_GENERIC_PROMPTS = (
    r"\.requestAuthorization\s*\(",
    r"\.requestAccess\s*\(",
    r"\brequestPermission\s*\(",
)
_DIRECT_PROMPT_CALL = re.compile(r"\.\s*prompt\s*\(\s*\)")


def kinds_for(name: str) -> list[PermissionKind]:
    """Every kind a free-form ``app_spec.permissions[].permission`` label mentions."""
    label = name.strip().lower()
    found: list[PermissionKind] = []
    for kind in PERMISSION_KINDS:
        tokens = (kind.case.replace("_", " "), kind.case, *kind.aliases)
        if any(re.search(rf"\b{re.escape(token)}s?\b", label) for token in tokens):
            found.append(kind)
    return found


def blank_comments_and_strings(source: str) -> str:
    """Replace comment and string-literal contents with spaces, keeping offsets and lines.

    A small Swift lexer: handles ``//`` and nested ``/* */`` comments and ``"..."`` /
    multi-line ``\"\"\"...\"\"\"`` strings with escapes, so a ``//`` inside a URL string
    no longer hides the code after it and prose inside strings is never matched.
    """
    out = list(source)
    i, n = 0, len(source)

    def blank(start: int, end: int) -> None:
        for j in range(start, min(end, n)):
            if out[j] != "\n":
                out[j] = " "

    while i < n:
        if source.startswith("//", i):
            end = source.find("\n", i)
            end = n if end == -1 else end
            blank(i, end)
            i = end
        elif source.startswith("/*", i):
            depth, j = 1, i + 2
            while j < n and depth:
                if source.startswith("/*", j):
                    depth, j = depth + 1, j + 2
                elif source.startswith("*/", j):
                    depth, j = depth - 1, j + 2
                else:
                    j += 1
            blank(i, j)
            i = j
        elif source.startswith('"""', i):
            end = source.find('"""', i + 3)
            end = n if end == -1 else end + 3
            blank(i + 3, end - 3)
            i = end
        elif source[i] == '"':
            j = i + 1
            while j < n and source[j] not in '"\n':
                j += 2 if source[j] == "\\" else 1
            blank(i + 1, j)
            i = j + 1
        else:
            i += 1
    return "".join(out)


def _line_of(source: str, offset: int) -> int:
    return source.count("\n", 0, offset) + 1


def _file_violations(rel: Path, source: str) -> list[str]:
    found: dict[int, str] = {}
    spans: list[tuple[int, int]] = []
    for kind in PERMISSION_KINDS:
        for pattern in kind.patterns:
            for match in re.finditer(pattern, source):
                spans.append(match.span())
                api = re.sub(r"\s+", "", match.group(0))[:60]
                found.setdefault(
                    match.start(),
                    f"{rel}:{_line_of(source, match.start())}: error: {kind.case} permission "
                    f"API `{api}` outside {PERMISSIONS_DIR}/ — move it into a PermissionPrompter "
                    "and call Permissions.request(...)",
                )
    for pattern in _GENERIC_PROMPTS:
        for match in re.finditer(pattern, source):
            if any(start <= match.start() < end for start, end in spans):
                continue
            found.setdefault(
                match.start(),
                f"{rel}:{_line_of(source, match.start())}: error: permission API "
                f"`{match.group(0).strip()}` outside {PERMISSIONS_DIR}/ — move it into a "
                "PermissionPrompter and call Permissions.request(...)",
            )
    for match in _DIRECT_PROMPT_CALL.finditer(source):
        found.setdefault(
            match.start(),
            f"{rel}:{_line_of(source, match.start())}: error: direct `.prompt()` call "
            "bypasses the headless gate — call Permissions.request(...) instead",
        )
    return [found[offset] for offset in sorted(found)]


def permission_violations(app_dir: Path) -> list[str]:
    """Prompting APIs used outside ``App/Permissions/`` (empty list = clean)."""
    violations: list[str] = []
    allowed = (app_dir / PERMISSIONS_DIR).resolve()
    for swift in sorted((app_dir / "App").rglob("*.swift")):
        if swift.resolve().is_relative_to(allowed):
            continue
        source = blank_comments_and_strings(swift.read_text(encoding="utf-8", errors="replace"))
        violations += _file_violations(swift.relative_to(app_dir), source)
    return violations
