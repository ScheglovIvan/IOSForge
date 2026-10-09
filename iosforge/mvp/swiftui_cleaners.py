"""Cleaner capability modules: duplicate photos, duplicate contacts, storage (Level 2 Phase D).

Contract code under ``App/Capabilities`` built on Apple frameworks only:

* ``photos_cleaner`` — ``PhotosCleaner``: fingerprints every library photo (difference
  hash of a 9x8 grey thumbnail), groups identical fingerprints and deletes the extra
  copies through PhotoKit (the system asks the user to confirm).
* ``contacts_cleaner`` — ``ContactsCleaner``: groups contacts by normalised full name (or
  phone number) and deletes the extra copies.
* ``storage_scan`` — ``StorageScan``: device capacity / free space and the app's own
  cache size, and clears that cache.

Access is always requested through the scaffold prompters (``Permissions.request``);
headless mode never touches the libraries. In functional mode the mocks grant access
up front (``simctl privacy``) and seed the simulator with known duplicates
(``simctl addmedia``); each module journals what it found and what it removed.
"""

from __future__ import annotations

import subprocess
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from PIL import Image, ImageDraw

from iosforge.mvp import swiftui_capabilities as caps
from iosforge.mvp.swiftui_functional import FunctionalCheck, MockContext, Step, register_mock
from iosforge.mvp.swiftui_templates import DO_NOT_EDIT

PHOTOS = "photos_cleaner"
CONTACTS = "contacts_cleaner"
STORAGE = "storage_scan"
SCAN_PATTERN = r"\b(scan|find|search|analy[sz]e|detect|check)\b"
CLEAN_PATTERN = r"\b(delete|clean|remove|merge|clear)\b|free up"
SCAN_ID = "iosforge.scan"
CLEAN_ID = "iosforge.clean"
MARK_RULE = (
    f'\n  Mark the scan button `.accessibilityIdentifier("{SCAN_ID}")` and the delete / clean /'
    f' merge\n  button `.accessibilityIdentifier("{CLEAN_ID}")`.'
)
PHOTOS_ALLOW = "Allow Full Access|Allow Access to All Photos|Allow|OK"
CONTACTS_ALLOW = "Allow Full Access|Continue|Allow|OK"

PHOTOS_SWIFT = f"""import Photos
import UIKit

/// A set of identical photos: the first is kept, the others are the duplicates.
struct PhotoDuplicateGroup: Identifiable {{
    let id = UUID()
    let assets: [PHAsset]

    var duplicates: Int {{ max(assets.count - 1, 0) }}
}}

/// Duplicate photo finder / remover on PhotoKit. Screens call only these functions.
/// {DO_NOT_EDIT}
@MainActor
enum PhotosCleaner {{
    struct Failure: LocalizedError {{
        let message: String
        var errorDescription: String? {{ message }}
    }}

    /// Scans the library (asks for access first) and returns the duplicate groups.
    static func scan() async throws -> [PhotoDuplicateGroup] {{
        guard !Headless.isActive else {{ return [] }}
        guard await Permissions.request(PhotosPermission.self) else {{
            Functional.record("photos.error", ["reason": "access denied"])
            throw Failure(message: "Photo access was not granted.")
        }}
        Functional.record("photos.scan_started")
        let options = PHFetchOptions()
        options.sortDescriptors = [NSSortDescriptor(key: "creationDate", ascending: true)]
        let result = PHAsset.fetchAssets(with: .image, options: options)
        var assets: [PHAsset] = []
        result.enumerateObjects {{ asset, _, _ in assets.append(asset) }}
        var buckets: [UInt64: [PHAsset]] = [:]
        for asset in assets {{
            if let value = await fingerprint(asset) {{
                buckets[value, default: []].append(asset)
            }}
        }}
        let groups = buckets.values.filter {{ $0.count > 1 }}.map {{ PhotoDuplicateGroup(assets: $0) }}
        let duplicates = groups.reduce(0) {{ $0 + $1.duplicates }}
        Functional.record("photos.scanned", ["photos": String(assets.count), "duplicates": String(duplicates)])
        if duplicates > 0 {{
            Functional.record("photos.duplicates_found", ["duplicates": String(duplicates)])
        }}
        return groups
    }}

    /// Deletes every duplicate (keeps the first photo of each group); returns how many.
    @discardableResult
    static func deleteDuplicates(in groups: [PhotoDuplicateGroup]) async throws -> Int {{
        let extras = groups.flatMap {{ $0.assets.dropFirst() }}
        guard !extras.isEmpty, !Headless.isActive else {{ return 0 }}
        try await PHPhotoLibrary.shared().performChanges {{
            PHAssetChangeRequest.deleteAssets(extras as NSArray)
        }}
        Functional.record("photos.deleted", ["count": String(extras.count)])
        return extras.count
    }}

    /// A thumbnail of `asset` for the screens.
    static func thumbnail(for asset: PHAsset, side: CGFloat = 160) async -> UIImage? {{
        await requestImage(asset, size: CGSize(width: side, height: side), mode: .opportunistic)
    }}

    private static func requestImage(
        _ asset: PHAsset, size: CGSize, mode: PHImageRequestOptionsDeliveryMode
    ) async -> UIImage? {{
        let options = PHImageRequestOptions()
        options.deliveryMode = mode == .opportunistic ? .highQualityFormat : mode
        options.resizeMode = .exact
        options.isNetworkAccessAllowed = false
        return await withCheckedContinuation {{ continuation in
            PHImageManager.default().requestImage(
                for: asset, targetSize: size, contentMode: .aspectFill, options: options
            ) {{ image, _ in
                continuation.resume(returning: image)
            }}
        }}
    }}

    private static func fingerprint(_ asset: PHAsset) async -> UInt64? {{
        guard let image = await requestImage(asset, size: CGSize(width: 64, height: 64), mode: .fastFormat),
              let cgImage = image.cgImage else {{ return nil }}
        var pixels = [UInt8](repeating: 0, count: 72)
        let drawn = pixels.withUnsafeMutableBytes {{ buffer -> Bool in
            guard let context = CGContext(
                data: buffer.baseAddress, width: 9, height: 8, bitsPerComponent: 8, bytesPerRow: 9,
                space: CGColorSpaceCreateDeviceGray(), bitmapInfo: CGImageAlphaInfo.none.rawValue
            ) else {{ return false }}
            context.interpolationQuality = .medium
            context.draw(cgImage, in: CGRect(x: 0, y: 0, width: 9, height: 8))
            return true
        }}
        guard drawn else {{ return nil }}
        var hash: UInt64 = 0
        for row in 0..<8 {{
            for column in 0..<8 {{
                hash <<= 1
                if pixels[row * 9 + column] > pixels[row * 9 + column + 1] {{ hash |= 1 }}
            }}
        }}
        return hash
    }}
}}
"""

CONTACTS_SWIFT = f"""import Contacts
import Foundation

/// Contacts that are the same person: the first is kept, the others are the duplicates.
struct ContactDuplicateGroup: Identifiable {{
    let id = UUID()
    let name: String
    let contacts: [CNContact]

    var duplicates: Int {{ max(contacts.count - 1, 0) }}
}}

/// Duplicate contact finder / merger on the Contacts framework. Screens call only these.
/// {DO_NOT_EDIT}
@MainActor
enum ContactsCleaner {{
    struct Failure: LocalizedError {{
        let message: String
        var errorDescription: String? {{ message }}
    }}

    private static let keys: [CNKeyDescriptor] = [
        CNContactGivenNameKey as CNKeyDescriptor,
        CNContactFamilyNameKey as CNKeyDescriptor,
        CNContactPhoneNumbersKey as CNKeyDescriptor,
        CNContactIdentifierKey as CNKeyDescriptor,
    ]

    /// Reads the contacts (asks for access first) and returns the duplicate groups.
    static func scan() async throws -> [ContactDuplicateGroup] {{
        guard !Headless.isActive else {{ return [] }}
        guard await Permissions.request(ContactsPermission.self) else {{
            Functional.record("contacts.error", ["reason": "access denied"])
            throw Failure(message: "Contacts access was not granted.")
        }}
        var all: [CNContact] = []
        try CNContactStore().enumerateContacts(with: CNContactFetchRequest(keysToFetch: keys)) {{ contact, _ in
            all.append(contact)
        }}
        var buckets: [String: [CNContact]] = [:]
        for contact in all {{
            let name = "\\(contact.givenName) \\(contact.familyName)"
                .trimmingCharacters(in: .whitespaces).lowercased()
            let phone = contact.phoneNumbers.first?.value.stringValue.filter(\\.isNumber) ?? ""
            let key = name.isEmpty ? phone : name
            guard !key.isEmpty else {{ continue }}
            buckets[key, default: []].append(contact)
        }}
        let groups = buckets.filter {{ $0.value.count > 1 }}
            .map {{ ContactDuplicateGroup(name: $0.key.capitalized, contacts: $0.value) }}
            .sorted {{ $0.name < $1.name }}
        let duplicates = groups.reduce(0) {{ $0 + $1.duplicates }}
        Functional.record("contacts.scanned", ["contacts": String(all.count), "duplicates": String(duplicates)])
        if duplicates > 0 {{
            Functional.record("contacts.duplicates_found", ["duplicates": String(duplicates)])
        }}
        return groups
    }}

    /// Deletes the extra copies of every group; returns how many contacts were removed.
    @discardableResult
    static func merge(_ groups: [ContactDuplicateGroup]) throws -> Int {{
        let extras = groups.flatMap {{ $0.contacts.dropFirst() }}
        guard !extras.isEmpty, !Headless.isActive else {{ return 0 }}
        let request = CNSaveRequest()
        for contact in extras {{
            if let mutable = contact.mutableCopy() as? CNMutableContact {{
                request.delete(mutable)
            }}
        }}
        try CNContactStore().execute(request)
        Functional.record("contacts.merged", ["count": String(extras.count)])
        return extras.count
    }}
}}
"""

STORAGE_SWIFT = f"""import Foundation

/// Device storage and the app's own cache, as the screens show it.
struct StorageSnapshot {{
    let total: Int64
    let available: Int64
    let cache: Int64

    var used: Int64 {{ max(total - available, 0) }}
}}

/// Storage scanner / cache cleaner on FileManager. Screens call only these.
/// {DO_NOT_EDIT}
enum StorageScan {{
    /// Capacity, free space and the app cache size.
    static func scan() throws -> StorageSnapshot {{
        let home = URL(fileURLWithPath: NSHomeDirectory())
        let values = try home.resourceValues(forKeys: [
            .volumeTotalCapacityKey, .volumeAvailableCapacityForImportantUsageKey,
        ])
        let snapshot = StorageSnapshot(
            total: Int64(values.volumeTotalCapacity ?? 0),
            available: values.volumeAvailableCapacityForImportantUsage ?? 0,
            cache: cacheFolders().reduce(0) {{ $0 + size(of: $1) }}
        )
        Functional.record("storage.scanned", ["total": String(snapshot.total), "cache": String(snapshot.cache)])
        return snapshot
    }}

    /// Clears the app's caches and temporary files; returns the bytes freed.
    @discardableResult
    static func cleanCache() throws -> Int64 {{
        guard !Headless.isActive else {{ return 0 }}
        var freed: Int64 = 0
        for folder in cacheFolders() {{
            let items = (try? FileManager.default.contentsOfDirectory(at: folder, includingPropertiesForKeys: nil)) ?? []
            for item in items {{
                freed += size(of: item)
                try? FileManager.default.removeItem(at: item)
            }}
        }}
        Functional.record("storage.cleaned", ["freed": String(freed)])
        return freed
    }}

    /// `bytes` as the system writes file sizes ("1.2 GB").
    static func format(_ bytes: Int64) -> String {{
        ByteCountFormatter.string(fromByteCount: bytes, countStyle: .file)
    }}

    private static func cacheFolders() -> [URL] {{
        let caches = FileManager.default.urls(for: .cachesDirectory, in: .userDomainMask)
        return caches + [FileManager.default.temporaryDirectory]
    }}

    private static func size(of url: URL) -> Int64 {{
        guard let enumerator = FileManager.default.enumerator(
            at: url, includingPropertiesForKeys: [.fileSizeKey]
        ) else {{
            return Int64((try? url.resourceValues(forKeys: [.fileSizeKey]).fileSize) ?? 0)
        }}
        var total: Int64 = 0
        for case let file as URL in enumerator {{
            total += Int64((try? file.resourceValues(forKeys: [.fileSizeKey]).fileSize) ?? 0)
        }}
        return total
    }}
}}
"""

PHOTOS_RULE = (
    "Duplicate photos only through the scaffold module (`App/Capabilities/PhotosCleaner.swift`):\n"
    "  a scan `Button` runs `let groups = try await PhotosCleaner.scan()` in a `Task` (it asks\n"
    "  for access itself), the screen lists the groups (`PhotoDuplicateGroup.assets`,\n"
    "  `PhotosCleaner.thumbnail(for:)`) and shows the duplicate count, and a delete / clean\n"
    "  `Button` calls `try await PhotosCleaner.deleteDuplicates(in: groups)`. Never import\n"
    "  Photos in screens and never fake results; in headless mode show the fixtures."
)
CONTACTS_RULE = (
    "Duplicate contacts only through the scaffold module (`App/Capabilities/ContactsCleaner.swift`):\n"
    "  a scan `Button` runs `let groups = try await ContactsCleaner.scan()` in a `Task` (it asks\n"
    "  for access itself), the screen lists the groups (`ContactDuplicateGroup.name`,\n"
    "  `.duplicates`) and a merge / delete `Button` calls `try ContactsCleaner.merge(groups)`.\n"
    "  Never import Contacts in screens and never fake results; headless shows fixtures."
)
STORAGE_RULE = (
    "Storage only through the scaffold module (`App/Capabilities/StorageScan.swift`): a scan /\n"
    "  check `Button` calls `let snapshot = try StorageScan.scan()` and shows `used`, `available`,\n"
    "  `total` and `cache` with `StorageScan.format(_:)`; a clean `Button` calls\n"
    "  `try StorageScan.cleanCache()`. Never read disk values in screens; headless shows fixtures."
)


def _photos_check(ctx: caps.CapabilityContext) -> FunctionalCheck | None:
    screen = caps.capability_screen(ctx)
    if not screen:
        return None
    return FunctionalCheck(
        name=str(ctx.capability.get("name") or PHOTOS),
        screen_id=screen,
        steps=(
            Step("tap", SCAN_PATTERN, timeout=10, identifier=SCAN_ID),
            Step("allow", PHOTOS_ALLOW, timeout=6),
            Step("pause", timeout=15),
            Step("tap", CLEAN_PATTERN, timeout=20, identifier=CLEAN_ID),
            Step("system", "Delete", timeout=15),
            Step("pause", timeout=3),
        ),
        expect_events=("photos.duplicates_found", "photos.deleted"),
        mock=PHOTOS,
    )


def _contacts_check(ctx: caps.CapabilityContext) -> FunctionalCheck | None:
    screen = caps.capability_screen(ctx)
    if not screen:
        return None
    return FunctionalCheck(
        name=str(ctx.capability.get("name") or CONTACTS),
        screen_id=screen,
        steps=(
            Step("tap", SCAN_PATTERN, timeout=10, identifier=SCAN_ID),
            Step("allow", CONTACTS_ALLOW, timeout=4),
            Step("pause", timeout=4),
            Step("tap", CLEAN_PATTERN, timeout=20, identifier=CLEAN_ID),
            Step("pause", timeout=3),
        ),
        expect_events=("contacts.duplicates_found", "contacts.merged"),
        mock=CONTACTS,
    )


def _storage_check(ctx: caps.CapabilityContext) -> FunctionalCheck | None:
    screen = caps.capability_screen(ctx)
    if not screen:
        return None
    return FunctionalCheck(
        name=str(ctx.capability.get("name") or STORAGE),
        screen_id=screen,
        steps=(
            Step("tap", SCAN_PATTERN + "|" + CLEAN_PATTERN, timeout=10, identifier=SCAN_ID),
            Step("pause", timeout=3),
        ),
        expect_events=("storage.scanned",),
    )


def _simctl(*args: str) -> None:
    subprocess.run(
        ["xcrun", "simctl", *args], capture_output=True, text=True, check=True, timeout=120
    )


def seed_photos(folder: Path) -> list[Path]:
    """Two distinct pictures and an exact copy of the first (one known duplicate)."""
    files: list[Path] = []
    for index, (a, b) in enumerate(
        (((220, 60, 60), (40, 40, 200)), ((30, 170, 90), (250, 220, 40)))
    ):
        image = Image.new("RGB", (480, 360), a)
        draw = ImageDraw.Draw(image)
        for step in range(0, 480, 60 + index * 25):
            draw.rectangle((step, 0, step + 20 + index * 15, 360), fill=b)
        path = folder / f"iosforge_seed_{index}.png"
        image.save(path)
        files.append(path)
    copy = folder / "iosforge_seed_0_copy.png"
    copy.write_bytes(files[0].read_bytes())
    return [*files, copy]


SEED_VCARD = (
    "BEGIN:VCARD\nVERSION:3.0\nN:Duplicate;Iosforge;;;\nFN:Iosforge Duplicate\n"
    "TEL;TYPE=CELL:+15550100\nEND:VCARD\n"
    "BEGIN:VCARD\nVERSION:3.0\nN:Duplicate;Iosforge;;;\nFN:Iosforge Duplicate\n"
    "TEL;TYPE=CELL:+15550100\nEND:VCARD\n"
    "BEGIN:VCARD\nVERSION:3.0\nN:Unique;Iosforge;;;\nFN:Iosforge Unique\n"
    "TEL;TYPE=CELL:+15550199\nEND:VCARD\n"
)


@contextmanager
def _photos_mock(check: FunctionalCheck, context: MockContext) -> Iterator[dict[str, str]]:
    _simctl("privacy", context.udid, "grant", "photos", context.bundle_id)
    with tempfile.TemporaryDirectory(prefix="iosforge-photos-") as tmp:
        _simctl("addmedia", context.udid, *[str(p) for p in seed_photos(Path(tmp))])
    yield {}


@contextmanager
def _contacts_mock(check: FunctionalCheck, context: MockContext) -> Iterator[dict[str, str]]:
    _simctl("privacy", context.udid, "grant", "contacts", context.bundle_id)
    with tempfile.TemporaryDirectory(prefix="iosforge-contacts-") as tmp:
        card = Path(tmp) / "iosforge_seed.vcf"
        card.write_text(SEED_VCARD, encoding="utf-8")
        _simctl("addmedia", context.udid, str(card))
    yield {}


def _usage(key: str, text: str) -> list[str]:
    return [f'        {key}: "{text}"']


DESCRIPTORS = (
    caps.CapabilityDescriptor(
        key=PHOTOS,
        directory=caps.CAPABILITIES_DIR,
        render=lambda ctx: {f"{caps.CAPABILITIES_DIR}/PhotosCleaner.swift": PHOTOS_SWIFT},
        screen_api_rule=PHOTOS_RULE + MARK_RULE,
        prompters=lambda ctx: ("photos",),
        info_properties=lambda ctx: _usage(
            "NSPhotoLibraryUsageDescription", "Find and remove duplicate photos to free up space."
        ),
        functional_check=_photos_check,
    ),
    caps.CapabilityDescriptor(
        key=CONTACTS,
        directory=caps.CAPABILITIES_DIR,
        render=lambda ctx: {f"{caps.CAPABILITIES_DIR}/ContactsCleaner.swift": CONTACTS_SWIFT},
        screen_api_rule=CONTACTS_RULE + MARK_RULE,
        prompters=lambda ctx: ("contacts",),
        info_properties=lambda ctx: _usage(
            "NSContactsUsageDescription", "Find and merge duplicate contacts."
        ),
        functional_check=_contacts_check,
    ),
    caps.CapabilityDescriptor(
        key=STORAGE,
        directory=caps.CAPABILITIES_DIR,
        render=lambda ctx: {f"{caps.CAPABILITIES_DIR}/StorageScan.swift": STORAGE_SWIFT},
        screen_api_rule=STORAGE_RULE + MARK_RULE,
        functional_check=_storage_check,
    ),
)


def register() -> None:
    """Register the cleaner modules and their seeding mocks."""
    for descriptor in DESCRIPTORS:
        caps.register(descriptor)
    register_mock(PHOTOS, _photos_mock)
    register_mock(CONTACTS, _contacts_mock)
