"""Cleaner capability modules: duplicate photos, duplicate contacts, storage (Level 2 Phase D).

Contract code under ``App/Capabilities`` built on Apple frameworks only:

* ``photos_cleaner`` — ``PhotosCleaner``: buckets library photos by pixel size and a
  difference hash of a 9x8 grey thumbnail (flat images skipped), confirms each bucket by
  a SHA-256 of the image data, and deletes the exact copies through PhotoKit (the system
  asks the user to confirm and keeps them in Recently Deleted).
* ``contacts_cleaner`` — ``ContactsCleaner``: groups cards with the same name that share a
  phone number or email, copies the missing numbers and emails into the kept card and
  deletes the others (screens must confirm first: the deletion is permanent).
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
CONFIRM_PATTERN = r"\b(merge|delete|confirm|yes)\b"
MARK_RULE = (
    f'\n  Mark the scan button `.accessibilityIdentifier("{SCAN_ID}")` and the delete / clean /'
    f' merge\n  button `.accessibilityIdentifier("{CLEAN_ID}")`.'
)
PHOTOS_ALLOW = "Allow Full Access|Allow Access to All Photos|Allow|OK"
CONTACTS_ALLOW = "Allow Full Access|Continue|Allow|OK"

PHOTOS_SWIFT = f"""import CryptoKit
import Photos
import UIKit

/// A set of byte-identical photos: the first (oldest) is kept, the others are the copies.
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

    /// Scans the library (asks for access first) and returns the groups of exact copies.
    /// Candidates share pixel size and a perceptual hash; a group is only formed from
    /// photos whose image data is identical, so similar shots are never treated as copies.
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
        var candidates: [String: [PHAsset]] = [:]
        for asset in assets {{
            guard let value = await fingerprint(asset), value != 0, value != UInt64.max else {{ continue }}
            candidates["\\(asset.pixelWidth)x\\(asset.pixelHeight):\\(value)", default: []].append(asset)
        }}
        var groups: [PhotoDuplicateGroup] = []
        for bucket in candidates.values where bucket.count > 1 {{
            var identical: [String: [PHAsset]] = [:]
            for asset in bucket {{
                if let digest = await digest(asset) {{
                    identical[digest, default: []].append(asset)
                }}
            }}
            groups += identical.values.filter {{ $0.count > 1 }}.map {{ PhotoDuplicateGroup(assets: $0) }}
        }}
        let duplicates = groups.reduce(0) {{ $0 + $1.duplicates }}
        Functional.record("photos.scanned", ["photos": String(assets.count), "duplicates": String(duplicates)])
        if duplicates > 0 {{
            Functional.record("photos.duplicates_found", ["duplicates": String(duplicates)])
        }}
        return groups
    }}

    /// Deletes every copy (keeps the first photo of each group); returns how many.
    /// The system asks the user to confirm and keeps the photos in Recently Deleted.
    @discardableResult
    static func deleteDuplicates(in groups: [PhotoDuplicateGroup]) async throws -> Int {{
        let extras = groups.flatMap {{ $0.assets.dropFirst() }}
        guard !extras.isEmpty, !Headless.isActive else {{ return 0 }}
        do {{
            try await PHPhotoLibrary.shared().performChanges {{
                PHAssetChangeRequest.deleteAssets(extras as NSArray)
            }}
        }} catch {{
            Functional.record("photos.error", ["reason": error.localizedDescription])
            throw error
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

    private static func digest(_ asset: PHAsset) async -> String? {{
        let options = PHImageRequestOptions()
        options.version = .current
        options.deliveryMode = .highQualityFormat
        options.isNetworkAccessAllowed = false
        let data: Data? = await withCheckedContinuation {{ continuation in
            PHImageManager.default().requestImageDataAndOrientation(for: asset, options: options) {{ data, _, _, _ in
                continuation.resume(returning: data)
            }}
        }}
        guard let data else {{ return nil }}
        return SHA256.hash(data: data).map {{ String(format: "%02x", $0) }}.joined()
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

/// Cards of the same person: same name and at least one shared phone number or email.
/// The first card is kept and receives the others' missing numbers and emails.
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

    /// Reads the contacts (asks for access first) and returns the duplicate groups.
    static func scan() async throws -> [ContactDuplicateGroup] {{
        guard !Headless.isActive else {{ return [] }}
        guard await Permissions.request(ContactsPermission.self) else {{
            Functional.record("contacts.error", ["reason": "access denied"])
            throw Failure(message: "Contacts access was not granted.")
        }}
        let (count, groups) = try await Task.detached {{ () throws -> (Int, [ContactDuplicateGroup]) in
            let all = try fetchAll()
            return (all.count, group(all))
        }}.value
        let duplicates = groups.reduce(0) {{ $0 + $1.duplicates }}
        Functional.record("contacts.scanned", ["contacts": String(count), "duplicates": String(duplicates)])
        if duplicates > 0 {{
            Functional.record("contacts.duplicates_found", ["duplicates": String(duplicates)])
        }}
        return groups
    }}

    /// Merges every group: copies the missing phone numbers and emails into the first card,
    /// then deletes the other cards. Deletion is permanent, so screens confirm first.
    /// Returns how many cards were removed.
    @discardableResult
    static func merge(_ groups: [ContactDuplicateGroup]) async throws -> Int {{
        let work = groups.filter {{ group in
            group.contacts.count > 1 && !group.contacts.contains {{ merged.contains($0.identifier) }}
        }}
        guard !work.isEmpty, !Headless.isActive else {{ return 0 }}
        do {{
            let (removed, copied) = try await Task.detached {{ try save(work) }}.value
            merged.formUnion(work.flatMap {{ $0.contacts.dropFirst().map(\\.identifier) }})
            Functional.record("contacts.merged", ["count": String(removed), "copied": String(copied)])
            return removed
        }} catch {{
            Functional.record("contacts.error", ["reason": error.localizedDescription])
            throw error
        }}
    }}

    private static var merged: Set<String> = []

    nonisolated private static var keys: [CNKeyDescriptor] {{
        [
            CNContactGivenNameKey as CNKeyDescriptor,
            CNContactFamilyNameKey as CNKeyDescriptor,
            CNContactPhoneNumbersKey as CNKeyDescriptor,
            CNContactEmailAddressesKey as CNKeyDescriptor,
            CNContactIdentifierKey as CNKeyDescriptor,
        ]
    }}

    nonisolated private static func fetchAll() throws -> [CNContact] {{
        var all: [CNContact] = []
        try CNContactStore().enumerateContacts(with: CNContactFetchRequest(keysToFetch: keys)) {{ contact, _ in
            all.append(contact)
        }}
        return all
    }}

    nonisolated private static func name(_ contact: CNContact) -> String {{
        "\\(contact.givenName) \\(contact.familyName)".trimmingCharacters(in: .whitespaces).lowercased()
    }}

    nonisolated private static func phoneKey(_ phone: CNPhoneNumber) -> String {{
        let digits = phone.stringValue.filter(\\.isNumber)
        return digits.isEmpty ? "" : "tel:" + String(digits.suffix(10))
    }}

    nonisolated private static func emailKey(_ email: NSString) -> String {{
        let value = (email as String).trimmingCharacters(in: .whitespaces).lowercased()
        return value.isEmpty ? "" : "mail:" + value
    }}

    nonisolated private static func handles(_ contact: CNContact) -> Set<String> {{
        var result = Set(contact.phoneNumbers.map {{ phoneKey($0.value) }})
        result.formUnion(contact.emailAddresses.map {{ emailKey($0.value) }})
        result.remove("")
        return result
    }}

    nonisolated private static func group(_ all: [CNContact]) -> [ContactDuplicateGroup] {{
        let named = Dictionary(grouping: all.filter {{ !name($0).isEmpty }}, by: name)
        var groups: [ContactDuplicateGroup] = []
        for (key, people) in named where people.count > 1 {{
            var clusters: [(contacts: [CNContact], handles: Set<String>)] = []
            for person in people {{
                let own = handles(person)
                guard !own.isEmpty else {{ continue }}
                var joined = (contacts: [person], handles: own)
                for index in clusters.indices.reversed() where !clusters[index].handles.isDisjoint(with: own) {{
                    joined = (clusters[index].contacts + joined.contacts, clusters[index].handles.union(joined.handles))
                    clusters.remove(at: index)
                }}
                clusters.append(joined)
            }}
            groups += clusters.filter {{ $0.contacts.count > 1 }}
                .map {{ ContactDuplicateGroup(name: key.capitalized, contacts: $0.contacts) }}
        }}
        return groups.sorted {{ $0.name < $1.name }}
    }}

    nonisolated private static func save(_ groups: [ContactDuplicateGroup]) throws -> (Int, Int) {{
        let store = CNContactStore()
        let request = CNSaveRequest()
        var removed = 0
        var copied = 0
        for group in groups {{
            let current = try store.unifiedContact(withIdentifier: group.contacts[0].identifier, keysToFetch: keys)
            guard let keeper = current.mutableCopy() as? CNMutableContact else {{ continue }}
            var known = handles(keeper)
            for card in group.contacts.dropFirst() {{
                for phone in card.phoneNumbers where known.insert(phoneKey(phone.value)).inserted {{
                    keeper.phoneNumbers.append(CNLabeledValue(label: phone.label, value: phone.value))
                    copied += 1
                }}
                for email in card.emailAddresses where known.insert(emailKey(email.value)).inserted {{
                    keeper.emailAddresses.append(CNLabeledValue(label: email.label, value: email.value))
                    copied += 1
                }}
                if let extra = card.mutableCopy() as? CNMutableContact {{
                    request.delete(extra)
                    removed += 1
                }}
            }}
            request.update(keeper)
        }}
        try store.execute(request)
        return (removed, copied)
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
                let bytes = size(of: item)
                if (try? FileManager.default.removeItem(at: item)) != nil {{
                    freed += bytes
                }}
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
    "  `PhotosCleaner.thumbnail(for:)`) for review and shows the duplicate count, and a delete /\n"
    "  clean `Button` calls `try await PhotosCleaner.deleteDuplicates(in: groups)` (the system\n"
    "  confirms). Never import Photos in screens and never fake results; headless shows fixtures."
)
CONTACTS_RULE = (
    "Duplicate contacts only through the scaffold module (`App/Capabilities/ContactsCleaner.swift`):\n"
    "  a scan `Button` runs `let groups = try await ContactsCleaner.scan()` in a `Task` (it asks\n"
    "  for access itself), the screen lists the groups (`ContactDuplicateGroup.name`,\n"
    "  `.duplicates`). The merge `Button` only opens an `.alert` that says the extra cards are\n"
    '  deleted after their numbers and emails are copied; the alert\'s destructive `Button("Merge")`\n'
    "  runs `try await ContactsCleaner.merge(groups)` in a `Task` and then clears `groups`.\n"
    "  Never merge without that confirmation, never import Contacts in screens and\n"
    "  never fake results; headless shows fixtures."
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
            Step("pause", timeout=6),
            Step("tap", CLEAN_PATTERN, timeout=20, identifier=CLEAN_ID),
            Step("confirm", CONFIRM_PATTERN, timeout=10),
            Step("pause", timeout=6),
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
    "TEL;TYPE=CELL:+1 555 0100\nEMAIL:seed@iosforge.dev\nEND:VCARD\n"
    "BEGIN:VCARD\nVERSION:3.0\nN:Duplicate;Iosforge;;;\nFN:Iosforge Duplicate\n"
    "TEL;TYPE=CELL:+15550177\nEND:VCARD\n"
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
