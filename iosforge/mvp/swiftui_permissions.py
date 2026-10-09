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
#: Scaffold directories of capability modules: trusted contract code the lint skips.
MODULE_DIRS = ("App/Capabilities", "App/Monetization")


@dataclass(frozen=True)
class PermissionKind:
    """One prompting capability: Swift case, Info.plist keys and API patterns."""

    case: str
    plist_keys: tuple[str, ...]
    patterns: tuple[str, ...]
    aliases: tuple[str, ...] = ()
    capture: tuple[str, ...] = ()


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
        ),
        ("gps",),
        capture=(r"CLLocationUpdate\s*\.\s*liveUpdates", r"CLServiceSession\b"),
    ),
    PermissionKind(
        "camera",
        ("NSCameraUsageDescription",),
        (r"requestAccess\s*\(\s*for:\s*(?:AVMediaType)?\s*\.video",),
        capture=(
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
        ),
        ("mic", "audio input", "voice recording"),
        capture=(r"AVAudioRecorder\s*\(", r"\.inputNode\b"),
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
        (),
        ("coremotion", "fitness", "pedometer"),
        capture=(
            r"CMMotionActivityManager\s*\(",
            r"CMPedometer\s*\(",
            r"CMAltimeter\s*\(",
            r"CMSensorRecorder\s*\(",
        ),
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
        (),
        capture=(r"CBCentralManager\s*\(", r"CBPeripheralManager\s*\("),
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
        (),
        ("homekit",),
        capture=(r"HMHomeManager\s*\(",),
    ),
    PermissionKind(
        "local_network",
        ("NSLocalNetworkUsageDescription",),
        (),
        ("bonjour",),
        capture=(r"NWBrowser\s*\(", r"NetServiceBrowser\s*\(", r"NWListener\s*\("),
    ),
    PermissionKind(
        "nearby_interaction",
        ("NSNearbyInteractionUsageDescription",),
        (),
        ("uwb",),
        capture=(r"NISession\s*\(",),
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
        (),
        capture=(r"NFC\w*ReaderSession\s*\(",),
    ),
    PermissionKind(
        "pasteboard",
        (),
        (
            r"UIPasteboard\s*\.\s*general\s*\.\s*(?:string|strings|image|images|url|urls|items)\b"
            r"(?!\s*=[^=])",
        ),
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
_GATE_CALL = re.compile(r"Permissions\s*\.\s*request\s*\(\s*(\w+)\s*\.\s*self")
_HEADLESS_GUARD = re.compile(
    r"guard\s+!\s*Headless\s*\.\s*isActive\s+else|if\s+Headless\s*\.\s*isActive\s*\{"
)
SERVICE_SUFFIX = "Service.swift"


def kinds_for(name: str) -> list[PermissionKind]:
    """Every kind a free-form ``app_spec.permissions[].permission`` label mentions."""
    label = name.strip().lower()
    found: list[PermissionKind] = []
    for kind in PERMISSION_KINDS:
        tokens = (kind.case.replace("_", " "), kind.case, *kind.aliases)
        if any(re.search(rf"\b{re.escape(token)}s?\b", label) for token in tokens):
            found.append(kind)
    return found


def lex_swift(source: str) -> list[tuple[str, int, int]]:
    """Comment and string-literal spans of Swift ``source`` as ``(kind, start, end)``.

    ``kind`` is ``"comment"`` (whole ``//`` / nested ``/* */`` comment) or ``"string"``
    (the literal's contents, quotes excluded; ``"..."`` and multi-line
    ``\"\"\"...\"\"\"`` with escapes).
    """
    spans: list[tuple[str, int, int]] = []
    i, n = 0, len(source)
    while i < n:
        if source.startswith("//", i):
            end = source.find("\n", i)
            end = n if end == -1 else end
            spans.append(("comment", i, end))
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
            spans.append(("comment", i, j))
            i = j
        elif source.startswith('"""', i):
            end = source.find('"""', i + 3)
            end = n if end == -1 else end
            spans.append(("string", i + 3, end))
            i = end + 3
        elif source[i] == '"':
            j = i + 1
            while j < n and source[j] not in '"\n':
                j += 2 if source[j] == "\\" else 1
            spans.append(("string", i + 1, j))
            i = j + 1
        else:
            i += 1
    return spans


def blank_comments_and_strings(source: str) -> str:
    """Replace comment and string-literal contents with spaces, keeping offsets and lines.

    Built on :func:`lex_swift`, so a ``//`` inside a URL string no longer hides the
    code after it and prose inside strings is never matched.
    """
    out = list(source)
    for _, start, end in lex_swift(source):
        for j in range(start, min(end, len(source))):
            if out[j] != "\n":
                out[j] = " "
    return "".join(out)


def line_of(source: str, offset: int) -> int:
    """1-based line number of ``offset`` in ``source``."""
    return source.count("\n", 0, offset) + 1


def _overlaps(span: tuple[int, int], spans: list[tuple[int, int]]) -> bool:
    return any(span[0] < end and start < span[1] for start, end in spans)


def _file_violations(
    rel: Path, source: str, *, declared: set[str] | None, service: bool
) -> list[str]:
    found: dict[int, str] = {}
    spans: list[tuple[int, int]] = []
    move = (
        f"move it into a PermissionPrompter in {PERMISSIONS_DIR}/ and call Permissions.request(...)"
    )
    for kind in PERMISSION_KINDS:
        for pattern in kind.patterns:
            for match in re.finditer(pattern, source):
                spans.append(match.span())
                api = re.sub(r"\s+", "", match.group(0))[:60]
                found.setdefault(
                    match.start(),
                    f"{rel}:{line_of(source, match.start())}: error: {kind.case} permission "
                    f"API `{api}` outside {PERMISSIONS_DIR}/ — {move}",
                )
        for pattern in kind.capture:
            for match in re.finditer(pattern, source):
                spans.append(match.span())
                if service and _HEADLESS_GUARD.search(source):
                    continue
                api = re.sub(r"\s+", "", match.group(0))[:60]
                hint = (
                    "this service must not start in headless mode — guard it with "
                    "`Headless.isActive`"
                    if service
                    else f"move it into a `*{SERVICE_SUFFIX}` that checks `Headless.isActive` and "
                    "starts only after Permissions.request(...) returned true"
                )
                found.setdefault(
                    match.start(),
                    f"{rel}:{line_of(source, match.start())}: error: {kind.case} capture "
                    f"API `{api}` — {hint}",
                )
    for pattern in _GENERIC_PROMPTS:
        for match in re.finditer(pattern, source):
            if _overlaps(match.span(), spans):
                continue
            found.setdefault(
                match.start(),
                f"{rel}:{line_of(source, match.start())}: error: permission API "
                f"`{match.group(0).strip()}` outside {PERMISSIONS_DIR}/ — {move}",
            )
    for match in _DIRECT_PROMPT_CALL.finditer(source):
        found.setdefault(
            match.start(),
            f"{rel}:{line_of(source, match.start())}: error: direct `.prompt()` call "
            "bypasses the headless gate — call Permissions.request(...) instead",
        )
    if declared is not None:
        for match in _GATE_CALL.finditer(source):
            if match.group(1) not in declared:
                found.setdefault(
                    match.start(),
                    f"{rel}:{line_of(source, match.start())}: error: `{match.group(1)}` is not "
                    "declared in app_spec.permissions — available prompters: "
                    f"{', '.join(sorted(declared)) or 'none'}",
                )
    return [found[offset] for offset in sorted(found)]


def permission_violations(app_dir: Path, *, declared: set[str] | None = None) -> list[str]:
    """Prompting APIs used outside ``App/Permissions/`` (empty list = clean).

    Explicit request APIs are only allowed in ``App/Permissions/`` and in the scaffold's
    capability modules (:data:`MODULE_DIRS`, contract code); capture APIs that
    prompt implicitly are also allowed in ``*Service.swift`` files that check
    ``Headless.isActive``. ``declared`` (prompter type names) additionally flags
    ``Permissions.request(X.self)`` for a kind the app_spec does not declare.
    """
    violations: list[str] = []
    trusted = [(app_dir / d).resolve() for d in (PERMISSIONS_DIR, *MODULE_DIRS)]
    for swift in sorted((app_dir / "App").rglob("*.swift")):
        if any(swift.resolve().is_relative_to(folder) for folder in trusted):
            continue
        source = blank_comments_and_strings(swift.read_text(encoding="utf-8", errors="replace"))
        violations += _file_violations(
            swift.relative_to(app_dir),
            source,
            declared=declared,
            service=swift.name.endswith(SERVICE_SUFFIX),
        )
    return violations
